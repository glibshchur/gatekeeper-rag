"""Row-level security tests.

These are the tests that decide whether this project's central claim is true. Each one
asserts against rows the *database* returned, using a connection that cannot bypass RLS.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekeeper.core.db import PRINCIPAL_GUC, app_engine, principal_session
from gatekeeper.core.models import Document
from gatekeeper.core.principal import Clearance

from .conftest import Fixture

pytestmark = pytest.mark.integration

PROTECTED_TABLES = ("tenants", "principals", "documents", "chunks", "policies", "audit_log")


async def visible_titles(principal: object, tenant_filter: bool = False) -> set[str]:
    async with principal_session(principal) as session:  # type: ignore[arg-type]
        rows = (
            await session.execute(select(Document.title).where(Document.source == "test"))
        ).all()
    return {r[0] for r in rows}


# --- the substrate ---------------------------------------------------------


async def test_app_role_cannot_bypass_rls() -> None:
    """If this fails, every other test in this file is meaningless."""
    async with async_sessionmaker(app_engine())() as session:
        row = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert row.rolsuper is False, "the query plane must not connect as a superuser"
    assert row.rolbypassrls is False, "the query plane must not hold BYPASSRLS"


async def test_rls_is_enabled_and_forced_on_every_protected_table() -> None:
    """ENABLE alone exempts the table owner. FORCE is what makes the guarantee real."""
    async with async_sessionmaker(app_engine())() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(:names)"
                ),
                {"names": list(PROTECTED_TABLES)},
            )
        ).all()
    found = {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows}
    for table in PROTECTED_TABLES:
        assert found[table] == (True, True), f"{table} is not fully protected"


async def test_a_session_without_claims_sees_nothing(fx: Fixture) -> None:
    """Fail closed. An unset GUC yields NULL, and `tenant_id = NULL` is false, so
    forgetting to set claims returns zero rows rather than the whole table."""
    async with async_sessionmaker(app_engine())() as session:
        count = (
            await session.execute(select(func.count(Document.id)).where(Document.source == "test"))
        ).scalar_one()
    assert count == 0


async def test_claims_do_not_survive_the_transaction(fx: Fixture) -> None:
    """set_config(..., is_local => true) must scope claims to the transaction, or a
    pooled connection would hand one user's authorization to the next."""
    async with principal_session(fx.executive) as session:
        inside = (
            await session.execute(text(f"SELECT current_setting('{PRINCIPAL_GUC}', true)"))
        ).scalar_one()
    assert inside

    async with async_sessionmaker(app_engine())() as session:
        after = (
            await session.execute(text(f"SELECT current_setting('{PRINCIPAL_GUC}', true)"))
        ).scalar_one()
    assert after in (None, ""), "authorization context leaked to the next transaction"


# --- the access model ------------------------------------------------------


async def test_group_membership_gates_documents(fx: Fixture) -> None:
    """Engineer and auditor share a tenant and a clearance, and differ only by group."""
    assert await visible_titles(fx.engineer) == {"public", "eng"}
    assert await visible_titles(fx.auditor) == {"public", "audit"}


async def test_clearance_floor_gates_documents(fx: Fixture) -> None:
    """The auditor is in the `audit` group but lacks the clearance for board material."""
    assert "board" not in await visible_titles(fx.auditor)
    assert "board" in await visible_titles(fx.executive)


async def test_clearance_alone_does_not_grant_access(fx: Fixture) -> None:
    """A high clearance is a ceiling, not a key. Without the group, there is no access."""
    from gatekeeper.core.principal import Principal

    ungrouped = Principal(
        id=fx.executive.id,
        tenant_id=fx.tenant_a,
        external_id="ungrouped-exec",
        email="x@test.invalid",
        groups=["all"],
        clearance=Clearance.EXECUTIVE,
    )
    assert await visible_titles(ungrouped) == {"public"}


async def test_public_documents_need_no_group(fx: Fixture) -> None:
    assert "public" in await visible_titles(fx.engineer)
    assert "public" in await visible_titles(fx.auditor)


async def test_cross_tenant_isolation(fx: Fixture) -> None:
    """The outsider holds every group and the top clearance -- in the wrong tenant."""
    assert await visible_titles(fx.outsider) == {"other_tenant"}
    assert "eng" not in await visible_titles(fx.outsider)


async def test_targeted_fetch_of_a_forbidden_id_returns_nothing(fx: Fixture) -> None:
    """Knowing a document's primary key must not be sufficient to read it. This is the
    shape of the attack the Phase 2 red-team suite automates."""
    async with principal_session(fx.engineer) as session:
        row = (
            await session.execute(select(Document).where(Document.id == fx.doc_ids["board"]))
        ).scalar_one_or_none()
    assert row is None


async def test_forbidden_rows_are_invisible_to_aggregates(fx: Fixture) -> None:
    """Counts must not leak existence. If COUNT(*) saw filtered rows, an attacker could
    enumerate a restricted corpus without ever reading a document."""
    async with principal_session(fx.engineer) as session:
        count = (
            await session.execute(select(func.count(Document.id)).where(Document.source == "test"))
        ).scalar_one()
    assert count == 2
