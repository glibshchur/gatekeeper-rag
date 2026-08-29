"""The ABAC engine, enforced by Postgres.

`tests/unit/test_oracle.py` pins the rules as written. This pins the SQL that implements
them, and the last test reconciles the two over every (principal, resource) pair in the
fixture — the same check the red-team suite runs over the whole corpus.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekeeper.core.db import admin_session, app_engine, principal_session
from gatekeeper.core.models import Document, Policy, Tenant
from gatekeeper.core.principal import Clearance, Principal
from gatekeeper.redteam.oracle import DenyRule, ResourceAttrs, is_entitled

pytestmark = pytest.mark.integration

DENY_RULES = [
    DenyRule(resource_tags_any=("pii",), employment_type_in=("contractor",)),
    DenyRule(resource_tags_any=("legal",), unless_groups_any=("legal",)),
]

# name              sens            groups        clr  tags             jurisdiction
DOCS = [
    ("open", "public", [], 0, [], []),
    ("staff-only", "internal", ["staff"], 1, [], []),
    ("comp", "restricted", ["staff"], 2, ["compensation"], []),
    ("hr-pii", "confidential", ["staff"], 1, ["pii"], []),
    ("both-tags", "restricted", ["staff"], 1, ["pii", "compensation"], []),
    ("nl-policy", "confidential", ["staff"], 1, [], ["NL"]),
    ("in-policy", "confidential", ["staff"], 1, [], ["IN"]),
    ("legal-hold", "confidential", ["staff"], 1, ["legal"], []),
]


@dataclass
class Fx:
    tenant_id: uuid.UUID
    people: dict[str, Principal]
    doc_ids: dict[str, uuid.UUID]


@pytest.fixture
async def fx() -> AsyncIterator[Fx]:
    tenant_id = uuid.uuid4()

    def who(handle: str, **kwargs: object) -> Principal:
        base: dict[str, object] = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "external_id": handle,
            "email": f"{handle}@test.invalid",
            "groups": ["staff"],
            "clearance": Clearance.EMPLOYEE,
        }
        return Principal(**(base | kwargs))  # type: ignore[arg-type]

    people = {
        "plain": who("plain", region="NL"),
        "hr": who(
            "hr", clearance=Clearance.MANAGER, region="NL", need_to_know=["pii", "compensation"]
        ),
        "roamer": who("roamer", region="NL", need_to_know=["global"]),
        "contractor": who(
            "contractor", region="NL", employment_type="contractor", need_to_know=["pii"]
        ),
        "counsel": who("counsel", groups=["staff", "legal"], need_to_know=["legal"]),
        "notcounsel": who("notcounsel", clearance=Clearance.EXECUTIVE, need_to_know=["legal"]),
        "lapsed": who(
            "lapsed",
            clearance=Clearance.EXECUTIVE,
            need_to_know=["pii", "compensation", "legal", "global"],
            valid_until=datetime.now(UTC) - timedelta(days=1),
        ),
    }

    doc_ids: dict[str, uuid.UUID] = {}
    async with admin_session() as session:
        session.add(Tenant(id=tenant_id, slug=f"abac-{tenant_id.hex[:8]}", name="ABAC"))
        await session.flush()
        for name, sens, groups, clr, tags, juris in DOCS:
            doc = Document(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                source="test",
                path=f"{name}.md",
                title=name,
                content_hash=f"{name:0<64}"[:64],
                sensitivity=sens,
                allowed_groups=groups,
                min_clearance=clr,
                need_to_know_tags=tags,
                jurisdiction=juris,
            )
            session.add(doc)
            doc_ids[name] = doc.id
        for i, rule in enumerate(
            [
                {"resource_tags_any": ["pii"], "employment_type_in": ["contractor"]},
                {"resource_tags_any": ["legal"], "unless_groups_any": ["legal"]},
            ]
        ):
            session.add(
                Policy(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=f"deny-{i}",
                    effect="deny",
                    predicate=rule,
                    enabled=True,
                )
            )

    yield Fx(tenant_id, people, doc_ids)

    async with admin_session() as session:
        await session.execute(delete(Policy).where(Policy.tenant_id == tenant_id))
        await session.execute(delete(Document).where(Document.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def visible(principal: Principal) -> set[str]:
    async with principal_session(principal) as session:
        stmt = select(Document.title).where(Document.source == "test")
        rows = (await session.execute(stmt)).all()
    return {r[0] for r in rows}


# --- need-to-know ----------------------------------------------------------


async def test_need_to_know_gates_tagged_documents(fx: Fx) -> None:
    assert "hr-pii" not in await visible(fx.people["plain"])
    assert "hr-pii" in await visible(fx.people["hr"])


async def test_need_to_know_requires_every_tag_not_any(fx: Fx) -> None:
    """`both-tags` needs pii AND compensation. The contractor holds pii alone."""
    partial = fx.people["contractor"].model_copy(update={"employment_type": "employee"})
    assert "both-tags" not in await visible(partial)
    assert "both-tags" in await visible(fx.people["hr"])


# --- jurisdiction ----------------------------------------------------------


async def test_jurisdiction_scopes_to_the_principals_region(fx: Fx) -> None:
    seen = await visible(fx.people["plain"])
    assert "nl-policy" in seen
    assert "in-policy" not in seen


async def test_global_grant_crosses_jurisdictions(fx: Fx) -> None:
    seen = await visible(fx.people["roamer"])
    assert {"nl-policy", "in-policy"} <= seen


# --- deny rules ------------------------------------------------------------


async def test_deny_rule_blocks_by_employment_type(fx: Fx) -> None:
    """The contractor and an equivalent employee differ only in employment_type."""
    employee = fx.people["contractor"].model_copy(update={"employment_type": "employee"})
    assert "hr-pii" in await visible(employee)
    assert "hr-pii" not in await visible(fx.people["contractor"])


async def test_deny_beats_allow_for_a_principal_who_passes_every_check(fx: Fx) -> None:
    """`notcounsel` has executive clearance, the group, and the legal grant. The hold
    still refuses them, because deny is evaluated before any grant is considered."""
    assert "legal-hold" not in await visible(fx.people["notcounsel"])
    assert "legal-hold" in await visible(fx.people["counsel"])


# --- expiry ----------------------------------------------------------------


async def test_expired_principal_cannot_even_produce_claims(fx: Fx) -> None:
    with pytest.raises(PermissionError, match="expired"):
        fx.people["lapsed"].to_claims()


async def test_expired_claims_are_refused_by_the_database(fx: Fx) -> None:
    """The application refuses to mint them; the database must refuse to honour them.
    A cached or replayed claims blob is the realistic way this happens."""
    lapsed = fx.people["lapsed"]
    forged = (
        lapsed.model_copy(update={"valid_until": None})
        .to_claims()
        .replace('"exp":null', '"exp":"2020-01-01T00:00:00+00:00"')
    )
    async with async_sessionmaker(app_engine())() as session, session.begin():
        await session.execute(
            text("SELECT set_config('gatekeeper.principal', :c, true)"), {"c": forged}
        )
        count = (
            await session.execute(select(func.count(Document.id)).where(Document.source == "test"))
        ).scalar_one()
    assert count == 0


# --- the reconciliation ----------------------------------------------------


async def test_database_and_oracle_agree_on_every_pair(fx: Fx) -> None:
    """Two independent implementations of the same written spec, compared exhaustively.

    This is the test that would catch a policy bug the targeted cases above miss: it
    does not depend on anyone having thought of the right scenario.
    """
    resources = {
        name: ResourceAttrs(
            tenant_id=fx.tenant_id,
            sensitivity=sens,
            allowed_groups=tuple(groups),
            min_clearance=clr,
            need_to_know_tags=tuple(tags),
            jurisdiction=tuple(juris),
        )
        for name, sens, groups, clr, tags, juris in DOCS
    }

    disagreements = []
    for handle, principal in fx.people.items():
        if principal.is_expired:
            continue
        actual = await visible(principal)
        expected = {
            name for name, res in resources.items() if is_entitled(principal, res, DENY_RULES)
        }
        if actual != expected:
            disagreements.append(f"{handle}: database={sorted(actual)} oracle={sorted(expected)}")
    assert not disagreements, "\n".join(disagreements)


async def test_the_coarse_predicate_removes_nothing_the_policy_permits(fx: Fx) -> None:
    """The safety property behind the ADR 0006 fix.

    `coarse_predicate()` restates two of the policy's clauses in the query so the planner
    can estimate and index them. That is only sound if the coarse predicate is *implied by*
    the policy — a superset. If it were ever narrower, queries would silently drop rows the
    principal is entitled to, and the failure would look like a retrieval quality problem
    rather than an authorization bug.

    Comparing the two result sets directly is the strongest available check: it does not
    depend on anyone reasoning correctly about which clauses are safe to restate.
    """
    from gatekeeper.retrieval.search import coarse_predicate

    for handle, principal in fx.people.items():
        if principal.is_expired:
            continue
        async with principal_session(principal) as session:
            policy_only = {
                row[0]
                for row in await session.execute(
                    select(Document.title).where(Document.source == "test")
                )
            }
            with_coarse = {
                row[0]
                for row in await session.execute(
                    select(Document.title)
                    .where(Document.source == "test")
                    .where(*coarse_predicate(Document, principal))
                )
            }
        assert with_coarse == policy_only, (
            f"{handle}: the coarse predicate changed the result set "
            f"(lost {sorted(policy_only - with_coarse)}, "
            f"gained {sorted(with_coarse - policy_only)})"
        )
