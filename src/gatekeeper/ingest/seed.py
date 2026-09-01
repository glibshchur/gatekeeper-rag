"""Seed the demo tenant and its cast of principals.

The cast is chosen so that every branch of the access model is reachable by someone, and
so that at least one pair of principals differs on exactly one attribute. Raj and Sam
share a clearance and a tenant and differ only by group membership; Dana and Mira differ
only by clearance and groups. That makes failures diagnosable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from gatekeeper.core.db import admin_session, unprincipaled_session
from gatekeeper.core.models import Policy, PrincipalRow, Tenant
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
        "need_to_know": [],
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
        "need_to_know": ["pii"],
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
        "need_to_know": ["security"],
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
        # `global` waives jurisdiction scoping: People Ops must be able to read every
        # entity's employment policy, not only the one they happen to sit in.
        "need_to_know": ["pii", "employment", "benefits", "global"],
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
        "need_to_know": [
            "compensation",
            "pii",
            "financial",
            "board",
            "strategy",
            "benefits",
            "global",
        ],
    },
    {
        "external_id": "pilar",
        "email": "pilar@contractor.example",
        "display_name": "Pilar Rus — Contract Engineer",
        "groups": ["all-employees", "engineering"],
        "clearance": Clearance.EMPLOYEE,
        "department": "engineering",
        "region": "ES",
        # Holds the pii grant, and is otherwise identical to Raj. That combination is
        # deliberate: without the grant she would be stopped by need-to-know before the
        # deny rule was ever consulted, and the rule would demonstrate nothing. With it,
        # every allow condition is satisfied and only `contractors-no-personal-data`
        # explains why she sees less than Raj.
        "employment_type": "contractor",
        "need_to_know": ["pii"],
    },
    {
        "external_id": "wren",
        "email": "wren@auditor.example",
        "display_name": "Wren Adeyemi — External Auditor (grant lapsed)",
        "groups": ["all-employees", "finance", "audit"],
        "clearance": Clearance.MANAGER,
        "department": "finance",
        "region": "US",
        "employment_type": "contractor",
        "need_to_know": ["financial"],
        # Deliberately in the past. Everything else about this principal says "allowed".
        "valid_until": datetime.now(UTC) - timedelta(days=3),
    },
)

# Deny rules, stored as data in the `policies` table rather than compiled into SQL.
# `gatekeeper.denied_tags()` collapses them into one tag set per statement.
DENY_POLICIES: tuple[dict[str, object], ...] = (
    {
        "name": "contractors-no-personal-data",
        "description": "Contractors may not read anything tagged pii, whatever else grants it.",
        "predicate": {"resource_tags_any": ["pii"], "employment_type_in": ["contractor"]},
        "priority": 10,
    },
    {
        "name": "litigation-hold-legal",
        "description": (
            "Active hold: legal material is restricted to the legal team, overriding the "
            "standing executive grant. Demonstrates deny beating allow for a principal "
            "who would otherwise pass every check."
        ),
        "predicate": {"resource_tags_any": ["legal"], "unless_groups_any": ["legal"]},
        "priority": 5,
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

        for rule in DENY_POLICIES:
            values = {**rule, "tenant_id": tenant_id, "effect": "deny", "enabled": True}
            dstmt = insert(Policy).values(**values)
            await session.execute(
                dstmt.on_conflict_do_update(
                    constraint="uq_policy_name",
                    set_={k: dstmt.excluded[k] for k in values if k not in ("tenant_id", "name")},
                )
            )
    return 1, len(CAST)


async def load_principal(external_id: str, tenant_slug: str = TENANT_SLUG) -> Principal:
    """Fetch a principal by handle. Resolving *who you are* necessarily precedes
    authorization, so this cannot run under the policy it is about to establish.

    It runs on the **app role** all the same. `gatekeeper.resolve_principal()` is a
    `SECURITY DEFINER` function taking an exact tenant slug and handle and returning at
    most one row: it cannot list, pattern-match or enumerate, so it discloses exactly what
    authenticating as that handle already discloses.

    This used to open an `admin_session()`, which put an RLS-bypassing credential in the
    API process — on the single hottest path there is, since `auth.resolve()` calls this
    on every request. See `docs/THREAT_MODEL.md` A3 and migration 0013.
    """
    async with unprincipaled_session() as session:
        row = (
            await session.execute(
                text("SELECT * FROM gatekeeper.resolve_principal(:tenant_slug, :external_id)"),
                {"tenant_slug": tenant_slug, "external_id": external_id},
            )
        ).one_or_none()
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
            need_to_know=list(row.need_to_know),
            valid_until=row.valid_until,
        )
