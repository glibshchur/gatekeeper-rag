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

from sqlalchemy import text
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


async def dispose_engines() -> None:
    global _app_engine, _owner_engine
    for engine in (_app_engine, _owner_engine):
        if engine is not None:
            await engine.dispose()
    _app_engine = None
    _owner_engine = None


@asynccontextmanager
async def principal_session(principal: Principal) -> AsyncIterator[AsyncSession]:
    """Open a transaction scoped to ``principal``.

    ``set_config(..., is_local => true)`` ties the claims to this transaction, so the
    setting cannot leak to the next checkout of a pooled connection. The claims are
    passed as a bind parameter rather than interpolated -- ``SET`` does not accept
    parameters, which is exactly why this uses ``set_config`` instead.
    """
    session_factory = async_sessionmaker(app_engine(), expire_on_commit=False)
    async with session_factory() as session, session.begin():
        await session.execute(
            text(f"SELECT set_config('{PRINCIPAL_GUC}', :claims, true)"),
            {"claims": principal.to_claims()},
        )
        yield session


@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    """Open an unfiltered transaction as the table owner. Ingestion and migrations only."""
    session_factory = async_sessionmaker(owner_engine(), expire_on_commit=False)
    async with session_factory() as session, session.begin():
        yield session
