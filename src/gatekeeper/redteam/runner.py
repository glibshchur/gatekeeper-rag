"""Run the adversarial corpus and score the access model against an independent oracle.

Four kinds of probe, in increasing order of how much they prove:

1. **Retrieval probes** — adversarially phrased queries run as an under-privileged
   principal. Every returned chunk is checked against the oracle.
2. **Direct fetch** — select a known-restricted document by primary key. Retrieval
   ranking is bypassed entirely, so this tests the policy and nothing else.
3. **Aggregate probes** — `COUNT(*)` must not see rows `SELECT` cannot. A count that
   leaks lets an attacker enumerate a corpus they can never read.
4. **Full-corpus reconciliation** — for every (principal, chunk) pair, the database's
   answer must equal the oracle's. This is the strongest of the four by a wide margin:
   the first three sample, this one is exhaustive.

Both failure directions are reported. Leak rate alone is satisfied by a system that
returns nothing; over-block rate alone is satisfied by one that returns everything.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekeeper.core.db import admin_session, app_engine, principal_session
from gatekeeper.core.models import Chunk, Document, Policy, Tenant
from gatekeeper.core.principal import Principal
from gatekeeper.ingest import seed
from gatekeeper.llm.embeddings import Embedder
from gatekeeper.redteam.oracle import DenyRule, ResourceAttrs, explain, is_entitled
from gatekeeper.retrieval.search import search, unfiltered_candidates

logger = logging.getLogger(__name__)
CORPUS_PATH = Path(__file__).parent / "attacks.yaml"


@dataclass
class Leak:
    kind: str
    category: str
    attacker: str
    query: str
    path: str
    reason: str


@dataclass
class Report:
    probes: int = 0
    leaks: list[Leak] = field(default_factory=list)
    overblocks: int = 0
    entitled_opportunities: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    reconciled_pairs: int = 0
    reconciliation_mismatches: list[str] = field(default_factory=list)
    aggregate_probes: int = 0
    fetch_probes: int = 0

    @property
    def leak_rate(self) -> float:
        return len(self.leaks) / self.probes if self.probes else 0.0

    @property
    def overblock_rate(self) -> float:
        return self.overblocks / self.entitled_opportunities if self.entitled_opportunities else 0.0

    @property
    def passed(self) -> bool:
        return not self.leaks and not self.reconciliation_mismatches


def build_probes(spec: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(category, attacker, query) for every template x target-phrase x attacker."""
    probes: list[tuple[str, str, str]] = []
    for template in spec["templates"]:
        for target in spec["targets"]:
            for phrase in target["phrases"]:
                query = " ".join(template["pattern"].format(phrase=phrase).split())
                for attacker in spec["attackers"]:
                    probes.append((template["category"], attacker, query))
    return probes


async def load_deny_rules(tenant_id: UUID) -> list[DenyRule]:
    async with admin_session() as session:
        rows = (
            await session.execute(
                select(Policy).where(
                    Policy.tenant_id == tenant_id, Policy.effect == "deny", Policy.enabled
                )
            )
        ).scalars()
        return [
            DenyRule(
                resource_tags_any=tuple(r.predicate.get("resource_tags_any", [])),
                unless_groups_any=tuple(r.predicate.get("unless_groups_any", [])),
                employment_type_in=tuple(r.predicate.get("employment_type_in", [])),
            )
            for r in rows
        ]


async def _chunk_attrs(chunk_ids: list[UUID]) -> dict[UUID, tuple[ResourceAttrs, str]]:
    """Attributes and path for specific chunks, read on the admin plane.

    The oracle needs the resource's real attributes to judge a decision, and by
    definition the attacker's session cannot supply them for a chunk it was denied.
    """
    if not chunk_ids:
        return {}
    async with admin_session() as session:
        rows = (
            await session.execute(
                select(
                    Chunk.id,
                    Chunk.tenant_id,
                    Chunk.sensitivity,
                    Chunk.allowed_groups,
                    Chunk.min_clearance,
                    Chunk.need_to_know_tags,
                    Chunk.jurisdiction,
                    Document.path,
                )
                .join(Document, Document.id == Chunk.document_id)
                .where(Chunk.id.in_(chunk_ids))
            )
        ).all()
    return {
        row.id: (
            ResourceAttrs(
                tenant_id=row.tenant_id,
                sensitivity=row.sensitivity,
                allowed_groups=tuple(row.allowed_groups),
                min_clearance=row.min_clearance,
                need_to_know_tags=tuple(row.need_to_know_tags),
                jurisdiction=tuple(row.jurisdiction),
            ),
            row.path,
        )
        for row in rows
    }


async def run_retrieval_probes(
    probes: list[tuple[str, str, str]],
    principals: dict[str, Principal],
    deny_rules: list[DenyRule],
    embedder: Embedder,
    report: Report,
    k: int = 10,
) -> None:
    for category, handle, query in probes:
        principal = principals[handle]
        # audit=False: the chain's per-tenant advisory lock would serialise the run and
        # bury the corpus in thousands of synthetic entries.
        result = await search(principal, query, embedder, k=k, count_withheld=True, audit=False)
        report.probes += 1

        returned = [UUID(c.chunk_id) for c in result.chunks]
        attrs = await _chunk_attrs(returned)

        for chunk_id in returned:
            entry = attrs.get(chunk_id)
            if entry is None:
                continue
            resource, path = entry
            reason = explain(principal, resource, deny_rules)
            if reason is not None:
                report.leaks.append(
                    Leak(
                        kind="retrieval",
                        category=category,
                        attacker=handle,
                        query=query,
                        path=path,
                        reason=reason,
                    )
                )
                report.by_category[category] = report.by_category.get(category, 0) + 1


async def run_overblock_probes(
    probes: list[tuple[str, str]],
    principals: dict[str, Principal],
    deny_rules: list[DenyRule],
    embedder: Embedder,
    report: Report,
    k: int = 10,
) -> None:
    """Measure authorization's cost to legitimate use, isolated from ranking quality.

    The comparison is against the *unfiltered* top-k, so a document that simply ranked
    poorly is not counted as over-blocked. Only a chunk the oracle permits, which the
    ranking did surface, and which the principal did not receive, counts.

    Caveat worth stating rather than burying: both sides are separate approximate
    searches, so a near-tie at the k boundary can register as an over-block that
    authorization had nothing to do with. The measured 1.15% is therefore an upper
    bound on real over-blocking, not a point estimate. Making it exact means running
    brute-force ground truth here too, as `evals/filtered_ann.py` does -- affordable for
    ten queries, not for the whole probe corpus.
    """
    for handle, query in probes:
        principal = principals[handle]
        result = await search(principal, query, embedder, k=k, audit=False)
        visible = {UUID(c.chunk_id) for c in result.chunks}

        candidates = await unfiltered_candidates(principal, query, embedder, k=k)
        attrs = await _chunk_attrs([UUID(c.chunk_id) for c in candidates])

        for candidate in candidates:
            entry = attrs.get(UUID(candidate.chunk_id))
            if entry is None:
                continue
            resource, _ = entry
            if is_entitled(principal, resource, deny_rules):
                report.entitled_opportunities += 1
                if UUID(candidate.chunk_id) not in visible:
                    report.overblocks += 1


async def run_fetch_probes(
    principals: dict[str, Principal], deny_rules: list[DenyRule], report: Report
) -> None:
    """Knowing a primary key must not be enough. Bypasses ranking entirely."""
    async with admin_session() as session:
        restricted = (
            await session.execute(
                select(Document.id, Document.path)
                .where(Document.sensitivity == "restricted")
                .order_by(Document.path)
                .limit(25)
            )
        ).all()

    for handle, principal in principals.items():
        if principal.is_expired:
            continue
        async with principal_session(principal) as session:
            rows = (
                await session.execute(
                    select(Document.id, Document.path).where(
                        Document.id.in_([r.id for r in restricted])
                    )
                )
            ).all()
        report.fetch_probes += 1
        for row in rows:
            async with admin_session() as admin:
                attrs = (
                    await admin.execute(
                        select(
                            Document.tenant_id,
                            Document.sensitivity,
                            Document.allowed_groups,
                            Document.min_clearance,
                            Document.need_to_know_tags,
                            Document.jurisdiction,
                        ).where(Document.id == row.id)
                    )
                ).one()
            resource = ResourceAttrs(
                tenant_id=attrs.tenant_id,
                sensitivity=attrs.sensitivity,
                allowed_groups=tuple(attrs.allowed_groups),
                min_clearance=attrs.min_clearance,
                need_to_know_tags=tuple(attrs.need_to_know_tags),
                jurisdiction=tuple(attrs.jurisdiction),
            )
            reason = explain(principal, resource, deny_rules)
            if reason is not None:
                report.leaks.append(
                    Leak("fetch-by-id", "id-fetch", handle, str(row.id), row.path, reason)
                )


async def run_aggregate_probes(
    principals: dict[str, Principal], report: Report, expected: dict[str, int]
) -> None:
    """COUNT(*) must not see rows SELECT cannot, or the corpus can be enumerated blind."""
    for handle, principal in principals.items():
        if principal.is_expired:
            continue
        async with principal_session(principal) as session:
            counted = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
        report.aggregate_probes += 1
        if counted != expected[handle]:
            report.leaks.append(
                Leak(
                    "aggregate",
                    "aggregate",
                    handle,
                    "SELECT count(*) FROM chunks",
                    "-",
                    f"count {counted} disagrees with oracle {expected[handle]}",
                )
            )


async def run_expired_grant_probe(report: Report, tenant_id: UUID) -> None:
    """An expired grant must fail in the database, not only in the application.

    ``Principal.to_claims()`` refuses to serialise one, so the only way to test the SQL
    side is to hand-craft the claims the way a stale cache or a compromised caller would.
    """
    stale = json.dumps(
        {
            "tenant": str(tenant_id),
            "principal": str(UUID(int=0)),
            "groups": ["all-employees", "finance", "audit"],
            "clearance": 3,
            "department": "finance",
            "region": "US",
            "employment_type": "employee",
            "need_to_know": ["financial", "compensation", "global"],
            "exp": "2020-01-01T00:00:00+00:00",
        }
    )

    async with async_sessionmaker(app_engine())() as session, session.begin():
        await session.execute(
            text("SELECT set_config('gatekeeper.principal', :c, true)"), {"c": stale}
        )
        visible = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
    report.aggregate_probes += 1
    if visible:
        report.leaks.append(
            Leak(
                "expired-grant",
                "expiry",
                "forged-stale-claims",
                "hand-crafted claims with exp in 2020",
                "-",
                f"{visible} chunks visible to an expired grant",
            )
        )


async def run_cross_tenant_probe(principals: dict[str, Principal], report: Report) -> None:
    """Every group, top clearance, every grant — in a tenant that does not exist."""
    template = principals["mira"]
    stranger = template.model_copy(update={"tenant_id": UUID(int=1), "external_id": "stranger"})
    async with principal_session(stranger) as session:
        visible = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
    report.aggregate_probes += 1
    if visible:
        report.leaks.append(
            Leak(
                "cross-tenant",
                "cross-tenant",
                "stranger",
                "count of all chunks",
                "-",
                f"{visible} chunks visible across a tenant boundary",
            )
        )


async def reconcile_full_corpus(
    principals: dict[str, Principal], deny_rules: list[DenyRule], report: Report
) -> dict[str, int]:
    """Compare the database's decision to the oracle's for every (principal, chunk) pair.

    The sampling probes above can only find what they happen to query. This cannot miss:
    if the SQL policy and the written spec disagree anywhere in the corpus, the counts
    differ. Returns the oracle's expected visible-chunk count per principal.
    """
    async with admin_session() as session:
        rows = (
            await session.execute(
                select(
                    Chunk.tenant_id,
                    Chunk.sensitivity,
                    Chunk.allowed_groups,
                    Chunk.min_clearance,
                    Chunk.need_to_know_tags,
                    Chunk.jurisdiction,
                )
            )
        ).all()

    resources = [
        ResourceAttrs(
            tenant_id=r.tenant_id,
            sensitivity=r.sensitivity,
            allowed_groups=tuple(r.allowed_groups),
            min_clearance=r.min_clearance,
            need_to_know_tags=tuple(r.need_to_know_tags),
            jurisdiction=tuple(r.jurisdiction),
        )
        for r in rows
    ]

    expected: dict[str, int] = {}
    for handle, principal in principals.items():
        if principal.is_expired:
            expected[handle] = 0
            continue
        count = sum(1 for res in resources if is_entitled(principal, res, deny_rules))
        expected[handle] = count
        report.reconciled_pairs += len(resources)

        async with principal_session(principal) as session:
            actual = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
        if actual != count:
            report.reconciliation_mismatches.append(
                f"{handle}: database says {actual:,}, oracle says {count:,} "
                f"({abs(actual - count):,} pairs disagree)"
            )
    return expected


async def run(embedder: Embedder, k: int = 10, quick: bool = False) -> Report:
    spec = yaml.safe_load(CORPUS_PATH.read_text())
    report = Report()

    async with admin_session() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == seed.TENANT_SLUG))
        ).scalar_one()

    deny_rules = await load_deny_rules(tenant_id)
    handles = [str(m["external_id"]) for m in seed.CAST]
    principals = {h: await seed.load_principal(h) for h in handles}

    expected = await reconcile_full_corpus(principals, deny_rules, report)
    await run_aggregate_probes(principals, report, expected)
    await run_expired_grant_probe(report, tenant_id)
    await run_cross_tenant_probe(principals, report)
    await run_fetch_probes(principals, deny_rules, report)

    probes = build_probes(spec)
    if quick:
        probes = probes[:: max(1, len(probes) // 20)]
    await run_retrieval_probes(probes, principals, deny_rules, embedder, report, k=k)

    legit = [(entry["who"], entry["query"]) for entry in spec["legitimate"]]
    await run_overblock_probes(legit, principals, deny_rules, embedder, report, k=k)

    return report


def main() -> None:  # pragma: no cover - convenience entry point
    from gatekeeper.config import get_settings
    from gatekeeper.llm.embeddings import build_embedder

    settings = get_settings()
    report = asyncio.run(run(build_embedder(settings.embedding_backend, settings.openai_api_key)))
    print(report)
