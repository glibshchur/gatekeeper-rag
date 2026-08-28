"""The audit chain, end to end: append, verify, and detect tampering."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select, text, update

from gatekeeper.core.audit import GENESIS, ChainBreakError, append, verify_chain
from gatekeeper.core.db import admin_session, principal_session
from gatekeeper.core.models import AuditEntry, PrincipalRow, Tenant
from gatekeeper.core.principal import Clearance, Principal

pytestmark = pytest.mark.integration


@pytest.fixture
async def actor() -> AsyncIterator[Principal]:
    tenant_id = uuid.uuid4()
    principal = Principal(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        external_id="auditor",
        email="a@test.invalid",
        groups=["staff"],
        clearance=Clearance.EMPLOYEE,
    )
    async with admin_session() as session:
        session.add(Tenant(id=tenant_id, slug=f"aud-{tenant_id.hex[:8]}", name="Audit"))
        await session.flush()
        session.add(
            PrincipalRow(
                id=principal.id,
                tenant_id=tenant_id,
                external_id="auditor",
                email=principal.email,
                groups=["staff"],
                clearance=1,
            )
        )
    yield principal
    async with admin_session() as session:
        await session.execute(delete(AuditEntry).where(AuditEntry.tenant_id == tenant_id))
        await session.execute(delete(PrincipalRow).where(PrincipalRow.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def write(actor: Principal, n: int) -> None:
    for i in range(n):
        async with principal_session(actor) as session:
            await append(session, actor, action="search", query_text=f"query {i}")


async def test_the_first_entry_links_to_genesis(actor: Principal) -> None:
    await write(actor, 1)
    async with admin_session() as session:
        entry = (
            await session.execute(select(AuditEntry).where(AuditEntry.tenant_id == actor.tenant_id))
        ).scalar_one()
    assert entry.prev_hash == GENESIS


async def test_a_clean_chain_verifies(actor: Principal) -> None:
    await write(actor, 5)
    async with admin_session() as session:
        assert await verify_chain(session, actor.tenant_id) == 5


async def test_editing_an_entry_breaks_verification(actor: Principal) -> None:
    """The realistic threat: someone with database access quietly rewrites what they
    searched for. The row still looks plausible; the chain does not."""
    await write(actor, 5)
    async with admin_session() as session:
        target = (
            await session.execute(
                select(AuditEntry.id)
                .where(AuditEntry.tenant_id == actor.tenant_id)
                .order_by(AuditEntry.id)
                .offset(2)
                .limit(1)
            )
        ).scalar_one()
        await session.execute(
            update(AuditEntry).where(AuditEntry.id == target).values(query_text="something else")
        )

    async with admin_session() as session:
        with pytest.raises(ChainBreakError) as exc:
            await verify_chain(session, actor.tenant_id)
    assert exc.value.entry_id == target
    assert "contents" in exc.value.reason


async def test_deleting_an_entry_breaks_the_links(actor: Principal) -> None:
    await write(actor, 5)
    async with admin_session() as session:
        target = (
            await session.execute(
                select(AuditEntry.id)
                .where(AuditEntry.tenant_id == actor.tenant_id)
                .order_by(AuditEntry.id)
                .offset(2)
                .limit(1)
            )
        ).scalar_one()
        await session.execute(delete(AuditEntry).where(AuditEntry.id == target))

    async with admin_session() as session:
        with pytest.raises(ChainBreakError) as exc:
            await verify_chain(session, actor.tenant_id)
    assert "link" in exc.value.reason


async def test_entries_are_written_in_the_querying_transaction(actor: Principal) -> None:
    """A rolled-back query must not leave an audit entry claiming it happened."""
    try:
        async with principal_session(actor) as session:
            await append(session, actor, action="search", query_text="doomed")
            raise RuntimeError("simulated failure after the audit write")
    except RuntimeError:
        pass

    async with admin_session() as session:
        rows = (
            (
                await session.execute(
                    select(AuditEntry).where(AuditEntry.tenant_id == actor.tenant_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_the_data_plane_cannot_rewrite_history(actor: Principal) -> None:
    """RLS grants the app role INSERT and SELECT on audit_log, never UPDATE or DELETE.
    Append-only is enforced by policy, not by convention."""
    await write(actor, 2)
    async with principal_session(actor) as session:
        result = await session.execute(
            text("UPDATE audit_log SET query_text = 'tampered' WHERE tenant_id = :t"),
            {"t": actor.tenant_id},
        )
        assert result.rowcount == 0

    async with admin_session() as session:
        assert await verify_chain(session, actor.tenant_id) == 2


async def test_another_tenants_entries_are_invisible(actor: Principal) -> None:
    await write(actor, 3)
    stranger = actor.model_copy(update={"tenant_id": uuid.uuid4()})
    async with principal_session(stranger) as session:
        rows = (await session.execute(select(AuditEntry))).scalars().all()
    assert rows == []
