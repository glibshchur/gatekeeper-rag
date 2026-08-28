from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, text

from gatekeeper.core.db import admin_session, dispose_engines
from gatekeeper.core.models import Document, PrincipalRow, Tenant
from gatekeeper.core.principal import Clearance, Principal, Sensitivity

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _fresh_engines() -> AsyncIterator[None]:
    """Each test runs in its own event loop, so asyncpg connections must not outlive it."""
    yield
    await dispose_engines()


@dataclass
class Fixture:
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    engineer: Principal
    auditor: Principal
    executive: Principal
    outsider: Principal
    doc_ids: dict[str, uuid.UUID]


def _principal(
    tenant: uuid.UUID, handle: str, groups: list[str], clearance: Clearance
) -> Principal:
    return Principal(
        id=uuid.uuid4(),
        tenant_id=tenant,
        external_id=handle,
        email=f"{handle}@test.invalid",
        display_name=handle,
        groups=groups,
        clearance=clearance,
    )


@pytest.fixture
async def fx() -> AsyncIterator[Fixture]:
    """A hermetic two-tenant fixture. Does not depend on the handbook corpus being present."""
    suffix = os.urandom(4).hex()
    slug_a, slug_b = f"t-a-{suffix}", f"t-b-{suffix}"

    async with admin_session() as session:
        a = Tenant(id=uuid.uuid4(), slug=slug_a, name="Tenant A")
        b = Tenant(id=uuid.uuid4(), slug=slug_b, name="Tenant B")
        session.add_all([a, b])
        await session.flush()

        engineer = _principal(a.id, "engineer", ["all", "engineering"], Clearance.EMPLOYEE)
        auditor = _principal(a.id, "auditor", ["all", "audit"], Clearance.EMPLOYEE)
        executive = _principal(a.id, "exec", ["all", "engineering", "audit"], Clearance.EXECUTIVE)
        outsider = _principal(
            b.id, "outsider", ["all", "engineering", "audit"], Clearance.EXECUTIVE
        )

        for p in (engineer, auditor, executive, outsider):
            session.add(
                PrincipalRow(
                    id=p.id,
                    tenant_id=p.tenant_id,
                    external_id=p.external_id,
                    email=p.email,
                    display_name=p.display_name,
                    groups=p.groups,
                    clearance=int(p.clearance),
                )
            )

        specs = {
            # name              tenant  sensitivity              groups          clearance
            "public": (a.id, Sensitivity.PUBLIC, [], Clearance.EXTERNAL),
            "eng": (a.id, Sensitivity.INTERNAL, ["engineering"], Clearance.EMPLOYEE),
            "audit": (a.id, Sensitivity.CONFIDENTIAL, ["audit"], Clearance.EMPLOYEE),
            "board": (a.id, Sensitivity.RESTRICTED, ["audit"], Clearance.EXECUTIVE),
            "other_tenant": (b.id, Sensitivity.INTERNAL, ["engineering"], Clearance.EMPLOYEE),
        }
        doc_ids: dict[str, uuid.UUID] = {}
        for name, (tenant_id, sensitivity, groups, clearance) in specs.items():
            doc = Document(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                source="test",
                path=f"{name}.md",
                title=name,
                content_hash=f"{name:0<64}"[:64],
                sensitivity=sensitivity.value,
                allowed_groups=groups,
                min_clearance=int(clearance),
            )
            session.add(doc)
            doc_ids[name] = doc.id

    yield Fixture(a.id, b.id, engineer, auditor, executive, outsider, doc_ids)

    async with admin_session() as session:
        await session.execute(delete(Document).where(Document.tenant_id.in_([a.id, b.id])))
        await session.execute(delete(PrincipalRow).where(PrincipalRow.tenant_id.in_([a.id, b.id])))
        await session.execute(delete(Tenant).where(Tenant.id.in_([a.id, b.id])))
        await session.execute(text("SELECT 1"))
