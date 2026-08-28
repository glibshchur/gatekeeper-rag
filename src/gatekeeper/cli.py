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
from gatekeeper.core.models import Document
from gatekeeper.core.principal import Principal
from gatekeeper.ingest import handbook, seed

app = typer.Typer(no_args_is_help=True, add_completion=False, help="gatekeeper-rag")
corpus_app = typer.Typer(no_args_is_help=True, help="Fetch and load document corpora")
principals_app = typer.Typer(no_args_is_help=True, help="Inspect principals and their access")
app.add_typer(corpus_app, name="corpus")
app.add_typer(principals_app, name="principals")

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


if __name__ == "__main__":
    app()
