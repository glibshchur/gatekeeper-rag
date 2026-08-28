"""gatekeeper command line."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from gatekeeper.config import get_settings
from gatekeeper.core.db import admin_session, dispose_engines, principal_session
from gatekeeper.core.models import Chunk, Document
from gatekeeper.core.principal import Principal
from gatekeeper.ingest import handbook, pipeline, seed
from gatekeeper.llm.embeddings import build_embedder
from gatekeeper.llm.generation import build_generator
from gatekeeper.retrieval.search import SearchResult, search

app = typer.Typer(no_args_is_help=True, add_completion=False, help="gatekeeper-rag")
corpus_app = typer.Typer(no_args_is_help=True, help="Fetch and load document corpora")
principals_app = typer.Typer(no_args_is_help=True, help="Inspect principals and their access")
index_app = typer.Typer(no_args_is_help=True, help="Chunk, embed, and index documents")
app.add_typer(corpus_app, name="corpus")
app.add_typer(principals_app, name="principals")
app.add_typer(index_app, name="index")

console = Console()


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run one coroutine and always tear the connection pools down after it."""

    async def wrapper() -> T:
        try:
            return await coro
        finally:
            await dispose_engines()

    return asyncio.run(wrapper())


@corpus_app.command("fetch")
def corpus_fetch() -> None:
    """Shallow-clone the GitLab Handbook into corpus/sources/."""
    settings = get_settings()
    dest = settings.corpus_dir / handbook.SOURCE
    console.print(f"[dim]cloning {handbook.HANDBOOK_REPO} -> {dest}[/dim]")
    handbook.fetch(dest)
    files = len(list((dest / "content").rglob("*.md")))
    console.print(f"[green]ok[/green] {files:,} markdown files available")


@corpus_app.command("load")
def corpus_load(
    profile: Annotated[str, typer.Option(help="full | small")] = "full",
) -> None:
    """Load the corpus into Postgres with ACLs derived from corpus/acl_rules.yaml."""
    settings = get_settings()

    async def go() -> tuple[int, dict[str, int]]:
        await seed.seed_tenant_and_principals()
        return await handbook.load(
            clone_dir=settings.corpus_dir / handbook.SOURCE,
            tenant_slug=seed.TENANT_SLUG,
            rules_path=settings.acl_rules_path,
            profile=profile,
        )

    count, tally = _run(go())

    table = Table(title=f"Loaded {count:,} documents (profile={profile})", title_justify="left")
    table.add_column("ACL rule", style="cyan")
    table.add_column("documents", justify="right")
    for rule, n in tally.items():
        table.add_row(rule, f"{n:,}")
    console.print(table)


@principals_app.command("list")
def principals_list() -> None:
    """Show the seeded cast."""
    table = Table(title="Principals", title_justify="left")
    for col in ("handle", "name", "clearance", "region", "groups"):
        table.add_column(col, style="cyan" if col == "handle" else None)
    for member in seed.CAST:
        table.add_row(
            str(member["external_id"]),
            str(member["display_name"]),
            str(int(member["clearance"])),  # type: ignore[call-overload]
            str(member["region"] or "—"),
            ", ".join(member["groups"]) or "—",  # type: ignore[arg-type]
        )
    console.print(table)


@principals_app.command("show")
def principals_show(handle: str) -> None:
    """Show exactly what one principal can retrieve.

    Every count below comes from a query the database itself filtered. The process
    running this command connects as `gatekeeper_app`, which cannot bypass RLS.
    """

    async def go() -> tuple[Principal, int, list[tuple[str, int]], list[tuple[str, str]]]:
        principal = await seed.load_principal(handle)

        async with admin_session() as admin:
            total = (await admin.execute(select(func.count(Document.id)))).scalar_one()

        async with principal_session(principal) as session:
            by_sensitivity = [
                (row.sensitivity, row.n)
                for row in await session.execute(
                    select(Document.sensitivity, func.count(Document.id).label("n")).group_by(
                        Document.sensitivity
                    )
                )
            ]
            sample = [
                (row.title, row.path)
                for row in await session.execute(
                    select(Document.title, Document.path)
                    .where(Document.sensitivity.in_(("confidential", "restricted")))
                    .order_by(Document.path)
                    .limit(5)
                )
            ]
        return principal, total, by_sensitivity, sample

    principal, total, by_sensitivity, sample = _run(go())

    visible = sum(n for _, n in by_sensitivity)
    console.print(
        f"\n[bold]{principal.display_name}[/bold]  "
        f"[dim]clearance={int(principal.clearance)} region={principal.region or '—'} "
        f"groups={', '.join(principal.groups) or '—'}[/dim]"
    )
    console.print(
        f"sees [bold green]{visible:,}[/bold green] of [bold]{total:,}[/bold] documents "
        f"([bold red]{total - visible:,}[/bold red] withheld by the database)\n"
    )

    table = Table(show_header=True)
    table.add_column("sensitivity")
    table.add_column("visible", justify="right")
    for label in ("public", "internal", "confidential", "restricted"):
        n = next((c for s, c in by_sensitivity if s == label), 0)
        table.add_row(label, f"{n:,}")
    console.print(table)

    if sample:
        console.print("\n[dim]sample of restricted material this principal may read:[/dim]")
        for title, path in sample:
            console.print(f"  [cyan]{title}[/cyan] [dim]{path}[/dim]")


@index_app.command("build")
def index_build(
    backend: Annotated[str, typer.Option(help="local | openai[:model]")] = "",
    limit: Annotated[int, typer.Option(help="index at most N documents")] = 0,
    target_tokens: Annotated[int, typer.Option(help="0 = derive from the backend")] = 0,
    overlap_tokens: int = 64,
    force: Annotated[bool, typer.Option(help="re-chunk and re-embed everything")] = False,
) -> None:
    """Chunk and embed the loaded corpus. Idempotent: unchanged documents are skipped."""
    settings = get_settings()
    embedder = build_embedder(backend or settings.embedding_backend, settings.openai_api_key)
    console.print(
        f"[dim]backend={embedder.space.model} dim={embedder.space.dim} "
        f"context={embedder.space.max_tokens} tokens[/dim]"
    )

    report = _run(
        pipeline.build_index(
            embedder=embedder,
            clone_dir=settings.corpus_dir / handbook.SOURCE,
            tenant_slug=seed.TENANT_SLUG,
            target_tokens=target_tokens or None,
            overlap_tokens=overlap_tokens,
            limit=limit or None,
            force=force,
        )
    )
    table = Table(title="Index build", title_justify="left", show_header=False)
    for label, value in report.as_rows():
        table.add_row(label, value)
    console.print(table)


@index_app.command("repair")
def index_repair(
    backend: Annotated[str, typer.Option(help="local | openai[:model]")] = "",
    target_tokens: Annotated[int, typer.Option(help="0 = derive from the backend")] = 0,
    overlap_tokens: int = 64,
) -> None:
    """Re-chunk only the documents whose stored chunks violate the current parameters.

    Run this after changing chunker logic. Unlike `build --force` it does not re-embed
    documents that would chunk identically, which on this corpus is 87% of them.
    """
    settings = get_settings()
    embedder = build_embedder(backend or settings.embedding_backend, settings.openai_api_key)

    async def go() -> tuple[int, pipeline.IndexReport]:
        stale = await pipeline.documents_needing_rechunk(
            seed.TENANT_SLUG, embedder, target_tokens or None
        )
        if not stale:
            return 0, pipeline.IndexReport()
        report = await pipeline.build_index(
            embedder=embedder,
            clone_dir=settings.corpus_dir / handbook.SOURCE,
            tenant_slug=seed.TENANT_SLUG,
            target_tokens=target_tokens or None,
            overlap_tokens=overlap_tokens,
            document_ids=stale,
            force=True,
        )
        return len(stale), report

    stale_count, report = _run(go())
    if not stale_count:
        console.print("[green]Nothing to repair — every chunk is within the target.[/green]")
        return
    console.print(f"[dim]{stale_count:,} documents need re-chunking[/dim]")
    table = Table(title="Index repair", title_justify="left", show_header=False)
    for label, value in report.as_rows():
        table.add_row(label, value)
    console.print(table)


@index_app.command("stats")
def index_stats() -> None:
    """Chunk counts per embedding space, and how much of the corpus is indexed."""

    async def go() -> tuple[list[tuple[str, int, int]], int]:
        async with admin_session() as session:
            rows = [
                (row.embedding_model or "—", row.chunks, row.documents)
                for row in await session.execute(
                    select(
                        Chunk.embedding_model,
                        func.count(Chunk.id).label("chunks"),
                        func.count(func.distinct(Chunk.document_id)).label("documents"),
                    ).group_by(Chunk.embedding_model)
                )
            ]
            total = (await session.execute(select(func.count(Document.id)))).scalar_one()
        return rows, total

    rows, total = _run(go())
    table = Table(title=f"Indexed chunks ({total:,} documents in corpus)", title_justify="left")
    for col in ("embedding space", "chunks", "documents", "coverage"):
        table.add_column(col, justify="right" if col != "embedding space" else "left")
    for model, chunks, documents in rows:
        table.add_row(model, f"{chunks:,}", f"{documents:,}", f"{documents / total:.0%}")
    console.print(table)


@app.command("ask")
def ask(
    question: str,
    who: Annotated[str, typer.Option("--as", help="principal handle, e.g. raj")] = "raj",
    k: int = 8,
    backend: str = "",
    generate: Annotated[bool, typer.Option(help="synthesise an answer if a key is set")] = True,
) -> None:
    """Retrieve as a given principal, then answer with citations."""
    settings = get_settings()
    embedder = build_embedder(backend or settings.embedding_backend, settings.openai_api_key)

    async def go() -> tuple[Principal, SearchResult]:
        principal = await seed.load_principal(who)
        result = await search(principal, question, embedder, k=k, count_withheld=True)
        return principal, result

    principal, result = _run(go())

    console.print(
        f"\n[dim]asked as[/dim] [bold]{principal.display_name}[/bold] "
        f"[dim]clearance={int(principal.clearance)} "
        f"groups={', '.join(principal.groups) or '—'}[/dim]"
    )
    withheld = result.withheld
    console.print(
        f"[dim]{len(result.chunks)} sources in {result.latency_ms} ms"
        + (f" · [red]{withheld} withheld by authorization[/red]" if withheld else "")
        + "[/dim]\n"
    )

    if not result.chunks:
        console.print("[yellow]No accessible sources matched this question.[/yellow]")
        raise typer.Exit(0)

    generator = (
        build_generator(settings.openai_api_key, settings.anthropic_api_key) if generate else None
    )
    if generator is not None:
        answer = generator.answer(question, result.chunks)
        console.print(answer.text)
        if not answer.is_grounded:
            console.print("\n[yellow]⚠ the model cited no sources; treat as ungrounded[/yellow]")
        usage = f"{answer.input_tokens:,} in / {answer.output_tokens:,} out"
        console.print(f"\n[dim]{answer.model} · {usage}[/dim]")
    elif generate:
        console.print(
            "[yellow]No model provider configured — retrieval only.[/yellow] "
            "[dim]Set GK_OPENAI_API_KEY or GK_ANTHROPIC_API_KEY and "
            "`uv sync --extra providers` to synthesise answers.[/dim]"
        )

    table = Table(title="Sources", title_justify="left")
    table.add_column("#", justify="right")
    table.add_column("score", justify="right")
    table.add_column("sensitivity")
    table.add_column("source")
    for i, chunk in enumerate(result.chunks, start=1):
        table.add_row(str(i), f"{chunk.score:.3f}", chunk.sensitivity, chunk.label)
    console.print(table)


if __name__ == "__main__":
    app()
