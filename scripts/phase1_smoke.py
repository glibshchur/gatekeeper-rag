"""End-to-end smoke check for the Phase 1 pipeline.

Run after `make bootstrap`. Reports index coverage, the access surface per principal,
retrieval differentiation across the access boundary, and query latency.

The short-return measurement in section 4 is the interesting one. HNSW returns its
`ef_search` candidates, RLS removes the unauthorized rows, and top-k can come back with
fewer than k results even though more authorized matches exist deeper in the graph.
`hnsw.iterative_scan` is supposed to compensate. This counts how often it does not --
which is the Phase 2 filtered-ANN problem, stated as a number instead of a worry.

    uv run python scripts/phase1_smoke.py
"""

from __future__ import annotations

import asyncio
import statistics
import time

from rich.console import Console
from rich.table import Table
from sqlalchemy import distinct, func, select

from gatekeeper.config import get_settings
from gatekeeper.core.db import admin_session, dispose_engines, principal_session
from gatekeeper.core.models import Chunk, Document
from gatekeeper.core.principal import Principal
from gatekeeper.ingest import seed
from gatekeeper.llm.embeddings import Embedder, build_embedder
from gatekeeper.retrieval.search import search

console = Console()

HANDLES = ["guest", "raj", "sam", "dana", "mira"]

# Probe queries deliberately straddle the access boundary: some are answerable by
# everyone, some only by one group, and a few only by an executive.
PROBES = [
    "How much can I expense for a meal on a business trip?",
    "What is the equity refresh policy for executives?",
    "How do I report a security incident?",
    "What is the parental leave policy in the Netherlands?",
    "What is the board meeting cadence and who attends?",
    "How does the compensation review cycle work?",
    "What are the incident severity levels?",
    "Who approves a purchase over ten thousand dollars?",
    "What is the process for offboarding a team member?",
    "How does the vulnerability disclosure program work?",
]

K = 10


async def section_index_coverage(embedder: Embedder) -> None:
    async with admin_session() as session:
        documents = (await session.execute(select(func.count(Document.id)))).scalar_one()
        row = (
            await session.execute(
                select(
                    func.count(Chunk.id),
                    func.count(distinct(Chunk.document_id)),
                    func.avg(Chunk.token_count),
                    func.percentile_cont(0.5).within_group(Chunk.token_count),
                    func.max(Chunk.token_count),
                )
            )
        ).one()
        chunks, indexed_docs, mean_tokens, median_tokens, max_tokens = row
        target = int(embedder.space.max_tokens * 0.75)
        oversized = (
            await session.execute(select(func.count(Chunk.id)).where(Chunk.token_count > target))
        ).scalar_one()

    table = Table(title="1 · Index coverage", title_justify="left", show_header=False)
    table.add_row("documents in corpus", f"{documents:,}")
    table.add_row("documents indexed", f"{indexed_docs:,} ({indexed_docs / documents:.0%})")
    table.add_row("chunks", f"{chunks:,}")
    table.add_row("chunks per document", f"{chunks / max(indexed_docs, 1):.1f}")
    table.add_row(
        "tokens mean / median / max", f"{mean_tokens:.0f} / {median_tokens:.0f} / {max_tokens}"
    )
    table.add_row(
        f"over target ({target})",
        f"{oversized:,} ({oversized / max(chunks, 1):.1%})",
    )

    # The invariant that actually matters. Exceeding the *target* is a quality issue --
    # subword tokenizers are not additive, so a chunk assembled to 384 can measure a
    # little over. Exceeding the model's *context* is a correctness bug: the embedder
    # truncates silently, so the tail becomes unretrievable text that still occupies a
    # row and still returns a plausible-looking vector.
    limit = embedder.space.max_tokens
    within = max_tokens <= limit
    table.add_row(
        f"within {embedder.space.model} context ({limit})",
        "[green]pass[/green]"
        if within
        else f"[bold red]FAIL — largest chunk is {max_tokens} tokens[/bold red]",
    )
    console.print(table)
    console.print()


async def section_access_surface(principals: dict[str, Principal]) -> None:
    table = Table(title="2 · Access surface (rows the database returns)", title_justify="left")
    for col in (
        "principal",
        "documents",
        "chunks",
        "public",
        "internal",
        "confidential",
        "restricted",
    ):
        table.add_column(col, justify="right" if col != "principal" else "left")

    async with admin_session() as session:
        total_docs = (await session.execute(select(func.count(Document.id)))).scalar_one()
        total_chunks = (await session.execute(select(func.count(Chunk.id)))).scalar_one()

    for handle, principal in principals.items():
        async with principal_session(principal) as session:
            documents = (await session.execute(select(func.count(Document.id)))).scalar_one()
            chunks = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
            by_label = {
                row.sensitivity: row.n
                for row in await session.execute(
                    select(Chunk.sensitivity, func.count(Chunk.id).label("n")).group_by(
                        Chunk.sensitivity
                    )
                )
            }
        table.add_row(
            handle,
            f"{documents:,}",
            f"{chunks:,}",
            *[
                f"{by_label.get(label, 0):,}"
                for label in ("public", "internal", "confidential", "restricted")
            ],
        )
    table.add_row(
        "[dim]corpus total[/dim]",
        f"[dim]{total_docs:,}[/dim]",
        f"[dim]{total_chunks:,}[/dim]",
        "",
        "",
        "",
        "",
    )
    console.print(table)
    console.print()


async def section_retrieval(
    principals: dict[str, Principal], embedder: Embedder
) -> tuple[list[float], dict[str, int], dict[str, int]]:
    table = Table(
        title=f"3 · Retrieval differentiation (k={K}, cell = returned / withheld)",
        title_justify="left",
    )
    table.add_column("query", overflow="fold", max_width=42)
    for handle in principals:
        table.add_column(handle, justify="center")

    latencies: list[float] = []
    short_returns = dict.fromkeys(principals, 0)
    withheld_totals = dict.fromkeys(principals, 0)

    for probe in PROBES:
        cells = []
        for handle, principal in principals.items():
            started = time.monotonic()
            result = await search(principal, probe, embedder, k=K, count_withheld=True)
            latencies.append((time.monotonic() - started) * 1000)

            withheld = result.withheld or 0
            withheld_totals[handle] += withheld
            if len(result.chunks) < K:
                short_returns[handle] += 1
            cells.append(
                f"{len(result.chunks)}"
                + (f" / [red]{withheld}[/red]" if withheld else " / [dim]0[/dim]")
            )
        table.add_row(probe, *cells)

    console.print(table)
    console.print()
    return latencies, short_returns, withheld_totals


def section_short_returns(short_returns: dict[str, int], withheld: dict[str, int]) -> None:
    table = Table(
        title="4 · Filtered-ANN health — the Phase 2 problem, measured",
        title_justify="left",
    )
    for col in ("principal", f"queries returning < {K}", "total withheld"):
        table.add_column(col, justify="right" if col != "principal" else "left")
    for handle in short_returns:
        count = short_returns[handle]
        marker = "[green]0[/green]" if count == 0 else f"[yellow]{count}[/yellow]"
        table.add_row(handle, f"{marker} / {len(PROBES)}", f"{withheld[handle]:,}")
    console.print(table)
    console.print(
        "[dim]A short return means HNSW ran out of authorized candidates before k. "
        "With a corpus this size that is an index artifact, not a lack of matches.[/dim]\n"
    )


def section_latency(latencies: list[float]) -> None:
    ordered = sorted(latencies)
    table = Table(
        title="5 · Query latency (embed + search + withheld probe)",
        title_justify="left",
        show_header=False,
    )
    table.add_row("samples", f"{len(ordered):,}")
    table.add_row("p50", f"{statistics.median(ordered):.0f} ms")
    table.add_row("p95", f"{ordered[int(len(ordered) * 0.95)]:.0f} ms")
    table.add_row("max", f"{max(ordered):.0f} ms")
    console.print(table)


async def main() -> None:
    settings = get_settings()
    embedder = build_embedder(settings.embedding_backend, settings.openai_api_key)
    console.print(
        f"\n[bold]gatekeeper-rag · Phase 1 smoke check[/bold]  "
        f"[dim]{embedder.space.model} · {embedder.space.dim}d[/dim]\n"
    )

    principals = {handle: await seed.load_principal(handle) for handle in HANDLES}

    try:
        await section_index_coverage(embedder)
        await section_access_surface(principals)
        latencies, short_returns, withheld = await section_retrieval(principals, embedder)
        section_short_returns(short_returns, withheld)
        section_latency(latencies)
    finally:
        await dispose_engines()


if __name__ == "__main__":
    asyncio.run(main())
