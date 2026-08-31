"""Database access.

Every read of tenant data goes through :func:`principal_session`, which opens a
transaction and installs the caller's claims into a transaction-local GUC before any
query runs. Row-level security policies read that GUC. There is deliberately no way to
obtain a data-plane session without a Principal -- if you find yourself wanting one, you
want :func:`admin_session` and you should be able to justify it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gatekeeper.config import get_settings
from gatekeeper.core.principal import Principal

PRINCIPAL_GUC = "gatekeeper.principal"

_app_engine: AsyncEngine | None = None
_owner_engine: AsyncEngine | None = None
_unprepared_engine: AsyncEngine | None = None


def app_engine() -> AsyncEngine:
    """Data plane. Connects as a NOSUPERUSER role that cannot bypass RLS."""
    global _app_engine
    if _app_engine is None:
        _app_engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _app_engine


def owner_engine() -> AsyncEngine:
    """Admin plane: migrations and ingestion. Owns the tables, bypasses RLS."""
    global _owner_engine
    if _owner_engine is None:
        _owner_engine = create_async_engine(get_settings().database_owner_url, pool_pre_ping=True)
    return _owner_engine


def unprepared_engine() -> AsyncEngine:
    """Data plane with prepared-statement caching switched off. Benchmarks only.

    A cached plan is not invalidated by a change to a planner GUC. The filtered-ANN
    benchmark computes ground truth with ``enable_indexscan = off`` and then measures the
    index path with byte-identical SQL, so the cache hands back the sequential-scan plan
    and the "approximate" path silently runs the exact one. That reports the index as
    worthless and recall as a perfect 1.000 -- both wrong, and both plausible enough to
    publish. Nothing outside `gatekeeper.evals` should need this.
    """
    global _unprepared_engine
    if _unprepared_engine is None:
        # The asyncpg dialect takes this as a URL query parameter, not a kwarg.
        url = make_url(get_settings().database_url).update_query_dict(
            {"prepared_statement_cache_size": "0"}
        )
        _unprepared_engine = create_async_engine(url, pool_pre_ping=True)
    return _unprepared_engine


async def dispose_engines() -> None:
    global _app_engine, _owner_engine, _unprepared_engine
    for engine in (_app_engine, _owner_engine, _unprepared_engine):
        if engine is not None:
            await engine.dispose()
    _app_engine = None
    _owner_engine = None
    _unprepared_engine = None


@asynccontextmanager
async def principal_session(
    principal: Principal, engine: AsyncEngine | None = None
) -> AsyncIterator[AsyncSession]:
    """Open a transaction scoped to ``principal``.

    ``set_config(..., is_local => true)`` ties the claims to this transaction, so the
    setting cannot leak to the next checkout of a pooled connection. The claims are
    passed as a bind parameter rather than interpolated -- ``SET`` does not accept
    parameters, which is exactly why this uses ``set_config`` instead.
    """
    session_factory = async_sessionmaker(engine or app_engine(), expire_on_commit=False)
    async with session_factory() as session, session.begin():
        await session.execute(
            text(f"SELECT set_config('{PRINCIPAL_GUC}', :claims, true)"),
            {"claims": principal.to_claims()},
        )
        yield session


@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    """Open an unfiltered transaction as the table owner. Bypasses row-level security.

    Not only ingestion and migrations, despite what an earlier version of this docstring
    claimed. Three parts of the *request* path use it, and each is a deliberate choice
    worth knowing about:

    * the `count_withheld` baseline, which must be genuinely unfiltered or the count of
      denied rows silently under-reports every denial the policy caused;
    * the query cache, which holds chunk ids and entitlement hashes but no content;
    * `/api/jobs`, gated on the `gatekeeper.admin` claim.

    Chunk *content* is never read through this connection. But the API process does hold
    an RLS-bypassing credential, which bounds what a compromise of that process costs —
    see `docs/THREAT_MODEL.md`, adversary A3.
    """
    session_factory = async_sessionmaker(owner_engine(), expire_on_commit=False)
    async with session_factory() as session, session.begin():
        yield session
