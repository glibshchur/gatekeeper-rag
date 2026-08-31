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
cache_app = typer.Typer(no_args_is_help=True, help="Inspect and purge the query cache")
app.add_typer(cache_app, name="cache")

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


@index_app.command("reacl")
def index_reacl() -> None:
    """Re-apply corpus/acl_rules.yaml to existing chunks without re-embedding."""
    stats = _run(pipeline.reapply_acls(seed.TENANT_SLUG))
    table = Table(title="ACL reapply", title_justify="left", show_header=False)
    for label, value in stats.items():
        table.add_row(label, f"{value:,}")
    console.print(table)


@index_app.command("rescan")
def index_rescan() -> None:
    """Re-score every chunk against the current injection rules, without re-embedding."""
    stats = _run(pipeline.rescan_injection(seed.TENANT_SLUG))
    table = Table(title="Injection rescan", title_justify="left", show_header=False)
    for label, value in stats.items():
        table.add_row(label, f"{value:,}")
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


@app.command("eval")
def eval_retrieval(
    k: int = 10,
    backend: str = "",
    rerank: Annotated[bool, typer.Option(help="include cross-encoder arms")] = True,
    out: Annotated[str, typer.Option(help="write the markdown report here")] = "docs/ABLATION.md",
) -> None:
    """Run the retrieval ablation over the golden set."""
    from pathlib import Path

    from gatekeeper.evals import ablation as ablation_mod
    from gatekeeper.evals.harness import load_golden, validate_golden

    settings = get_settings()
    embedder = build_embedder(backend or settings.embedding_backend, settings.openai_api_key)

    reranker = None
    if rerank:
        from gatekeeper.llm.rerank import CrossEncoderReranker

        console.print("[dim]loading cross-encoder…[/dim]")
        reranker = CrossEncoderReranker()

    async def go() -> tuple[ablation_mod.Ablation, list[str]]:
        questions = load_golden()
        problems = await validate_golden(questions)
        if problems:
            return ablation_mod.Ablation([], k, 0, 0), problems
        async with admin_session() as session:
            corpus = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
        result = await ablation_mod.run(
            questions, embedder, k=k, reranker=reranker, corpus_chunks=corpus
        )
        return result, []

    result, problems = _run(go())
    if problems:
        console.print("[bold red]The golden set does not validate:[/bold red]")
        for line in problems:
            console.print(f"  {line}")
        raise typer.Exit(1)

    table = Table(
        title=f"Retrieval ablation ({result.question_count} questions)", title_justify="left"
    )
    for col in ("configuration", f"hit@{k}", f"recall@{k}", "MRR", f"nDCG@{k}", "p50"):
        table.add_column(col, justify="left" if col == "configuration" else "right")
    best = max(r.ndcg for r in result.reports)
    for report in result.reports:
        ndcg = (
            f"[bold green]{report.ndcg:.3f}[/bold green]"
            if report.ndcg == best
            else f"{report.ndcg:.3f}"
        )
        table.add_row(
            report.config.name,
            f"{report.hit_rate:.3f}",
            f"{report.recall:.3f}",
            f"{report.mrr:.3f}",
            ndcg,
            f"{report.p50_ms:.0f} ms",
        )
    console.print(table)

    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ablation_mod.to_markdown(result, embedder.space.model, reranker.model if reranker else None)
    )
    console.print(f"\n[dim]written to {path}[/dim]")


@app.command("bench")
def bench(
    k: int = 10,
    backend: str = "",
    out: Annotated[str, typer.Option(help="write the markdown report here")] = "docs/BENCHMARKS.md",
) -> None:
    """Measure what row-level security costs approximate nearest-neighbour search."""
    from pathlib import Path

    from gatekeeper.evals import filtered_ann

    settings = get_settings()
    embedder = build_embedder(backend or settings.embedding_backend, settings.openai_api_key)

    async def go() -> tuple[list[filtered_ann.Measurement], int]:
        async with admin_session() as session:
            corpus = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
        principals = {
            str(m["external_id"]): await seed.load_principal(str(m["external_id"]))
            for m in seed.CAST
        }
        return await filtered_ann.run(principals, embedder, corpus, k=k), corpus

    results, corpus = _run(go())

    before_after = Table(title="Selectivity cliff: coarse predicate off → on", title_justify="left")
    for col in ("principal", "selectivity", "plan (before)", "p50", "plan (after)", "p50"):
        before_after.add_column(
            col, justify="left" if "plan" in col or col == "principal" else "right"
        )
    befores = {m.principal: m for m in results if not m.coarse}
    afters = {
        m.principal: m
        for m in results
        if m.coarse and m.scan_mode == "relaxed_order" and m.ef_search == filtered_ann.DEFAULT_EF
    }
    for handle, b in befores.items():
        a = afters.get(handle)
        if a is None:
            continue
        gain = b.p50_ms / a.p50_ms if a.p50_ms else 0.0
        before_after.add_row(
            handle,
            f"{b.selectivity:.1%}",
            b.plan,
            f"{b.p50_ms:.0f} ms",
            a.plan,
            f"[green]{a.p50_ms:.0f} ms[/green]" if gain > 1.5 else f"{a.p50_ms:.0f} ms",
        )
    console.print(before_after)
    console.print()

    table = Table(title=f"Filtered-ANN recall@{k} (coarse predicate on)", title_justify="left")
    for col in (
        "principal",
        "selectivity",
        "iterative_scan",
        "ef",
        f"recall@{k}",
        "short",
        "p50",
        "exact p50",
    ):
        table.add_column(col, justify="left" if col in ("principal", "iterative_scan") else "right")
    for m in results:
        if not m.coarse:
            continue
        recall = (
            f"[green]{m.recall:.3f}[/green]"
            if m.recall >= 0.99
            else f"[yellow]{m.recall:.3f}[/yellow]"
            if m.recall >= 0.9
            else f"[red]{m.recall:.3f}[/red]"
        )
        table.add_row(
            m.principal,
            f"{m.selectivity:.1%}",
            m.scan_mode,
            str(m.ef_search),
            recall,
            str(m.short_returns),
            f"{m.p50_ms:.0f} ms",
            f"{m.exact_p50_ms:.0f} ms",
        )
    console.print(table)

    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        filtered_ann.to_markdown(results, k, corpus)
        + "\n"
        + filtered_ann.before_after_markdown(results, k)
    )
    console.print(f"\n[dim]written to {path}[/dim]")


@app.command("injection")
def injection(
    backend: str = "",
    semantic: Annotated[
        bool, typer.Option(help="report exemplar-similarity diagnostics (not used for scoring)")
    ] = True,
) -> None:
    """Score the injection classifier: detection on planted payloads, false positives on
    the real corpus.

    The second number is the one that decides whether this ships. Detection on attacks
    you wrote yourself is easy; a flag rate a reviewer can live with is not.
    """
    from gatekeeper.redteam import injection_eval

    settings = get_settings()
    embedder = (
        build_embedder(backend or settings.embedding_backend, settings.openai_api_key)
        if semantic
        else None
    )
    report = _run(injection_eval.run(embedder))

    summary = Table(title="Injection classifier", title_justify="left", show_header=False)
    detection = (
        f"[green]{report.detection_rate:.0%}[/green]"
        if report.detection_rate >= 0.8
        else f"[yellow]{report.detection_rate:.0%}[/yellow]"
    )
    fpr = (
        f"[green]{report.false_positive_rate:.3%}[/green]"
        if report.false_positive_rate < 0.005
        else f"[bold red]{report.false_positive_rate:.3%}[/bold red]"
    )
    summary.add_row("payloads detected", f"{report.detected}/{report.payloads}  {detection}")
    summary.add_row("corpus chunks scanned", f"{report.corpus_chunks:,}")
    summary.add_row("flagged (false positives)", f"{report.flagged:,}  {fpr}")
    summary.add_row("quarantined", f"{report.quarantined:,}")
    console.print(summary)

    if report.subtle_similarity and report.plain_similarity:
        lo_s, hi_s = min(report.subtle_similarity), max(report.subtle_similarity)
        lo_p, hi_p = min(report.plain_similarity), max(report.plain_similarity)
        console.print(
            f"\n[dim]exemplar similarity: evasive payloads {lo_s:.3f}-{hi_s:.3f}, "
            f"rule-detectable payloads {lo_p:.3f}-{hi_p:.3f}. The ranges overlap, which is "
            f"why the semantic layer was removed rather than tuned: no threshold separates "
            f"them.[/dim]"
        )

    if report.by_category:
        cats = Table(title="Detection by category", title_justify="left")
        cats.add_column("category")
        cats.add_column("detected", justify="right")
        for name, (hit, total) in sorted(report.by_category.items()):
            marker = "[green]" if hit == total else "[yellow]"
            cats.add_row(name, f"{marker}{hit}/{total}[/]")
        console.print(cats)

    if report.missed:
        console.print("\n[yellow]Missed payloads:[/yellow] " + ", ".join(report.missed))

    if report.worst:
        console.print(
            "\n[dim]highest-scoring corpus chunks (inspect these — they are the "
            "false positives a reviewer would see first):[/dim]"
        )
        for fp in report.worst:
            console.print(f"  [cyan]{fp.score:.3f}[/cyan] {fp.path}")
            console.print(f"        [dim]{', '.join(fp.signals)}[/dim]")
            console.print(f"        [dim]{fp.excerpt[:110]}[/dim]")


@app.command("redteam-indirect")
def redteam_indirect(
    k: int = 10,
    backend: str = "",
    clean: Annotated[bool, typer.Option(help="only remove planted rows, then exit")] = False,
) -> None:
    """Plant poisoned documents in the live corpus and attack through the real pipeline.

    Reports two rates that must not be confused: how often the classifier noticed, and
    how often the attack widened access. Only the second one has to be perfect.
    """
    from gatekeeper.redteam import indirect

    if clean:
        removed = _run(indirect.unplant())
        console.print(f"removed {removed} planted document(s)")
        return

    settings = get_settings()
    embedder = build_embedder(backend or settings.embedding_backend, settings.openai_api_key)
    report = _run(indirect.run(embedder, k=k))

    summary = Table(
        title="Indirect injection — live corpus", title_justify="left", show_header=False
    )
    summary.add_row("payloads planted", f"{report.planted}")
    summary.add_row("corpus size while planted", f"{report.corpus_chunks:,} chunks")
    summary.add_row("probes run", f"{report.probes_run}")
    summary.add_row("payloads that reached the model", f"{report.reached}")
    summary.add_row("of those, flagged by the classifier", f"{report.detected}")
    summary.add_row(
        "access widened",
        "[bold green]0 — contained[/bold green]"
        if report.contained
        else f"[bold red]{len(report.breaches)} BREACH[/bold red]",
    )
    summary.add_row(
        "reached, undetected, still contained",
        f"[green]{report.undetected_but_contained}[/green]",
    )
    console.print(summary)

    table = Table(title="Per payload", title_justify="left")
    for col in ("payload", "category", "reached", "flagged", "contained"):
        table.add_column(col, justify="left" if col in ("payload", "category") else "center")
    for a in report.attempts:
        name = f"{a.payload_id}{' *' if a.subtle else ''}"
        table.add_row(
            name,
            a.category,
            "yes" if a.retrieved else "[dim]no[/dim]",
            "[green]yes[/green]" if a.flagged else ("[yellow]no[/yellow]" if a.retrieved else "-"),
            "[bold red]NO[/bold red]" if a.widened else "[green]yes[/green]",
        )
    console.print(table)
    console.print("[dim]* written to evade the classifier[/dim]")

    if report.breaches:
        console.print("\n[bold red]Breaches:[/bold red]")
        for a in report.breaches:
            for w in a.widened[:3]:
                console.print(f"  {a.payload_id} -> {w}")
        raise typer.Exit(1)

    unreached = report.planted - report.reached
    console.print(
        "\n[green]Contained.[/green] No planted instruction widened what the attacker "
        "could read, including the payloads the classifier did not detect."
    )
    if unreached:
        console.print(
            f"[yellow]Caveat:[/yellow] {unreached} payload(s) never ranked against the "
            f"{report.corpus_chunks:,}-chunk corpus, so this run says nothing about them. "
            "Their headings are generic enough that real handbook pages outrank them — "
            "which is a property of the corpus, not evidence of a defence."
        )


@app.command("redteam")
def redteam(
    quick: Annotated[
        bool, typer.Option(help="sample the corpus instead of running it all")
    ] = False,
    k: int = 10,
    backend: str = "",
) -> None:
    """Run the adversarial corpus and score it against an independent oracle.

    Exits non-zero on any leak or reconciliation mismatch, so CI can gate on it.
    """
    from gatekeeper.redteam import runner

    settings = get_settings()
    embedder = build_embedder(backend or settings.embedding_backend, settings.openai_api_key)
    report = _run(runner.run(embedder, k=k, quick=quick))

    summary = Table(title="Red team", title_justify="left", show_header=False)
    summary.add_row("retrieval probes", f"{report.probes:,}")
    summary.add_row("direct-fetch probes", f"{report.fetch_probes:,}")
    summary.add_row("aggregate / boundary probes", f"{report.aggregate_probes:,}")
    summary.add_row("(principal, chunk) pairs reconciled", f"{report.reconciled_pairs:,}")
    summary.add_row(
        "leaks",
        f"[green]{len(report.leaks)}[/green]"
        if not report.leaks
        else f"[bold red]{len(report.leaks)}[/bold red]",
    )
    summary.add_row("leak rate", f"{report.leak_rate:.2%}")
    withheld = f"{report.overblocks}/{report.entitled_opportunities} entitled results withheld"
    summary.add_row("over-block rate", f"{report.overblock_rate:.2%} [dim]({withheld})[/dim]")
    console.print(summary)

    if report.reconciliation_mismatches:
        console.print("\n[bold red]Oracle disagrees with the database:[/bold red]")
        for line in report.reconciliation_mismatches:
            console.print(f"  {line}")

    if report.leaks:
        console.print("\n[bold red]Leaks:[/bold red]")
        for leak in report.leaks[:20]:
            console.print(f"  [{leak.kind}/{leak.category}] {leak.attacker} → {leak.path}")
            console.print(f"    [dim]{leak.reason}[/dim]")
        if len(report.leaks) > 20:
            console.print(f"  [dim]… and {len(report.leaks) - 20} more[/dim]")

    if not report.passed:
        raise typer.Exit(1)
    console.print("\n[green]No leaks. Database and oracle agree on every pair.[/green]")


@app.command("audit")
def audit_verify() -> None:
    """Verify the tamper-evidence of the audit chain."""
    from gatekeeper.core.audit import ChainBreakError, verify_chain

    async def go() -> tuple[int, str | None]:
        principal = await seed.load_principal("mira")
        async with admin_session() as session:
            try:
                return await verify_chain(session, principal.tenant_id), None
            except ChainBreakError as exc:
                return 0, str(exc)

    verified, error = _run(go())
    if error:
        console.print(f"[bold red]{error}[/bold red]")
        raise typer.Exit(1)
    console.print(f"[green]audit chain intact[/green] — {verified:,} entries verified")


@cache_app.command("purge")
def cache_purge(
    all_epochs: Annotated[
        bool, typer.Option("--all", help="drop every entry, not just retired ones")
    ] = False,
) -> None:
    """Delete query-cache entries from retired visibility epochs.

    Never required for correctness: the epoch is inside the fingerprint hash, so a retired
    entry is already unreachable. This only reclaims space.
    """
    from gatekeeper.retrieval import cache as query_cache

    removed = _run(query_cache.purge(keep_current=not all_epochs))
    console.print(f"removed {removed:,} cache entr{'y' if removed == 1 else 'ies'}")


@cache_app.command("stats")
def cache_stats() -> None:
    """Entries, hits and epochs."""
    from gatekeeper.core.models import CacheEpoch, QueryCacheEntry

    async def go() -> tuple[int, int, int, int, str]:
        from gatekeeper.retrieval import cache as query_cache

        epoch = await query_cache.current_epoch()
        async with admin_session() as session:
            entries = (await session.execute(select(func.count(QueryCacheEntry.id)))).scalar_one()
            live = (
                await session.execute(
                    select(func.count(QueryCacheEntry.id)).where(QueryCacheEntry.epoch == epoch)
                )
            ).scalar_one()
            hits = (
                await session.execute(select(func.coalesce(func.sum(QueryCacheEntry.hits), 0)))
            ).scalar_one()
            reason = (
                await session.execute(
                    select(CacheEpoch.reason).order_by(CacheEpoch.epoch.desc()).limit(1)
                )
            ).scalar_one_or_none()
        return epoch, entries, live, int(hits), reason or "-"

    epoch, entries, live, hits, reason = _run(go())
    table = Table(title="Query cache", title_justify="left", show_header=False)
    table.add_row("visibility epoch", f"{epoch} (last bumped by: {reason})")
    table.add_row("entries", f"{entries:,}")
    table.add_row("reachable (current epoch)", f"{live:,}")
    table.add_row("total hits served", f"{hits:,}")
    console.print(table)


@app.command("token")
def token(
    who: Annotated[str, typer.Option("--as", help="principal handle")],
    ttl: Annotated[int, typer.Option(help="lifetime in seconds")] = 3600,
    impersonate: Annotated[bool, typer.Option(help="grant the impersonation claim")] = False,
) -> None:
    """Mint a development bearer token for the console or the API.

    Prints only the token, so it pipes: `curl -H "Authorization: Bearer $(gatekeeper token
    --as raj)"`. Refused unless GK_AUTH_MODE is `dev`.
    """
    from gatekeeper.core import auth

    async def go() -> str:
        # Resolve first so a typo in the handle fails here rather than at the first
        # request with a valid-looking token.
        await seed.load_principal(who)
        return auth.issue_dev_token(who, ttl_seconds=ttl, can_impersonate=impersonate)

    try:
        print(_run(go()))
    except auth.AuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


@app.command("mcp")
def mcp(
    who: Annotated[str, typer.Option("--as", help="principal handle to bind this server to")] = "",
    rerank: Annotated[bool, typer.Option(help="load the cross-encoder")] = True,
) -> None:
    """Serve the knowledge base over MCP on stdio, scoped to one principal.

    Nothing may be written to stdout: it is the JSON-RPC channel. This command therefore
    prints nothing itself, and the server pins logging to stderr.
    """
    from gatekeeper.apps.mcp.server import main as serve_mcp

    serve_mcp(who or None, with_reranker=rerank)


@app.command("serve")
def serve(
    host: str = "127.0.0.1",
    port: int = 8077,
    reload: Annotated[bool, typer.Option(help="auto-reload on source changes")] = False,
) -> None:
    """Serve the demo console.

    Binds to loopback by default. The console impersonates principals without any
    credential check, so exposing it on 0.0.0.0 would hand anyone on the network the
    CFO's view of the corpus.
    """
    import uvicorn

    console.print(f"[green]console[/green] http://{host}:{port}  [dim](ctrl-c to stop)[/dim]")
    uvicorn.run(
        "gatekeeper.apps.api.main:app", host=host, port=port, reload=reload, log_level="warning"
    )


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
        # Citation markers are cheap to produce and prove nothing, so the claims are
        # checked against the sources rather than the brackets counted.
        from gatekeeper.llm.groundedness import annotate, verify

        report = verify(answer.text, result.chunks, embedder)
        style = "dim" if report.grounded else "yellow"
        console.print(f"\n[{style}]{annotate(report)}[/{style}]")

        usage = f"{answer.input_tokens:,} in / {answer.output_tokens:,} out"
        console.print(f"[dim]{answer.model} · {usage}[/dim]")
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
