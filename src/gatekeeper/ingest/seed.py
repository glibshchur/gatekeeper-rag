"""Seed the demo tenant and its cast of principals.

The cast is chosen so that every branch of the access model is reachable by someone, and
so that at least one pair of principals differs on exactly one attribute. Raj and Sam
share a clearance and a tenant and differ only by group membership; Dana and Mira differ
only by clearance and groups. That makes failures diagnosable.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from gatekeeper.core.db import admin_session
from gatekeeper.core.models import PrincipalRow, Tenant
from gatekeeper.core.principal import Clearance, Principal

TENANT_SLUG = "acme-corp"
TENANT_NAME = "Acme Corp (GitLab Handbook corpus)"

CAST: tuple[dict[str, object], ...] = (
    {
        "external_id": "guest",
        "email": "guest@example.com",
        "display_name": "Unauthenticated Guest",
        "groups": [],
        "clearance": Clearance.EXTERNAL,
        "department": None,
        "region": None,
        "employment_type": "external",
    },
    {
        "external_id": "raj",
        "email": "raj@acme.example",
        "display_name": "Raj Mehta — Backend Engineer",
        "groups": ["all-employees", "engineering"],
        "clearance": Clearance.EMPLOYEE,
        "department": "engineering",
        "region": "IN",
        "employment_type": "employee",
    },
    {
        "external_id": "sam",
        "email": "sam@acme.example",
        "display_name": "Sam Okafor — Security Engineer",
        "groups": ["all-employees", "engineering", "security"],
        "clearance": Clearance.EMPLOYEE,
        "department": "security",
        "region": "US",
        "employment_type": "employee",
    },
    {
        "external_id": "dana",
        "email": "dana@acme.example",
        "display_name": "Dana Voss — People Ops Manager",
        "groups": ["all-employees", "people-ops", "hiring-managers"],
        "clearance": Clearance.MANAGER,
        "department": "people-ops",
        "region": "NL",
        "employment_type": "employee",
    },
    {
        "external_id": "mira",
        "email": "mira@acme.example",
        "display_name": "Mira Lindqvist — CFO",
        "groups": ["all-employees", "finance", "executives", "comp-committee"],
        "clearance": Clearance.EXECUTIVE,
        "department": "finance",
        "region": "US",
        "employment_type": "employee",
    },
)


async def seed_tenant_and_principals() -> tuple[int, int]:
    async with admin_session() as session:
        tstmt = insert(Tenant).values(slug=TENANT_SLUG, name=TENANT_NAME)
        await session.execute(
            tstmt.on_conflict_do_update(index_elements=["slug"], set_={"name": TENANT_NAME})
        )
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == TENANT_SLUG))
        ).scalar_one()

        for member in CAST:
            values = {**member, "tenant_id": tenant_id, "clearance": int(member["clearance"])}  # type: ignore[call-overload]
            pstmt = insert(PrincipalRow).values(**values)
            await session.execute(
                pstmt.on_conflict_do_update(
                    constraint="uq_principal_external",
                    set_={
                        k: pstmt.excluded[k]
                        for k in values
                        if k not in ("tenant_id", "external_id")
                    },
                )
            )
    return 1, len(CAST)


async def load_principal(external_id: str, tenant_slug: str = TENANT_SLUG) -> Principal:
    """Fetch a principal by handle. Runs on the admin plane: resolving *who you are* is
    an authentication concern and necessarily precedes authorization."""
    async with admin_session() as session:
        row = (
            await session.execute(
                select(PrincipalRow)
                .join(Tenant, Tenant.id == PrincipalRow.tenant_id)
                .where(Tenant.slug == tenant_slug, PrincipalRow.external_id == external_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(f"no principal {external_id!r} in tenant {tenant_slug!r}")
        return Principal(
            id=row.id,
            tenant_id=row.tenant_id,
            external_id=row.external_id,
            email=row.email,
            display_name=row.display_name,
            groups=list(row.groups),
            clearance=Clearance(row.clearance),
            department=row.department,
            region=row.region,
            employment_type=row.employment_type,
            valid_until=row.valid_until,
        )
