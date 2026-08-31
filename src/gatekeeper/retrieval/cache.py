"""Semantic query cache that cannot serve one principal's results to another.

A cache is the most natural place in a system like this to introduce exactly the bug the
rest of it exists to prevent. Key a retrieval cache on the query — text or embedding — and
the first person to ask "what is the equity refresh policy" populates an entry that the
next person gets, regardless of what either is cleared to read. The failure is silent, it
looks like a performance win, and it survives every test that only checks latency.

Three decisions make this safe, and the second is the one worth stealing.

**1. Cache the decision, not the data.** An entry stores chunk *ids*, never chunk content.
A hit re-fetches those ids through `principal_session`, so the row-level security policy
runs again on every hit. Even if the key were wrong, a hit could not return a row the
principal is not entitled to — the worst case degrades to a wasted lookup. A cache holding
content would be a second copy of the corpus sitting outside RLS, and it is not worth the
milliseconds. What the cache actually saves is the embedding pass and the cross-encoder,
which are ~200 ms of the ~215 ms; the authorized fetch is 2-6 ms and is not worth skipping.

**2. Key on entitlement, not identity.** Keying on `principal_id` is safe and nearly
useless: in an enterprise, hundreds of people share a role, and a per-person cache never
hits. The key here is a hash of *exactly the attributes the policy consults* — tenant,
clearance, groups, need-to-know, region, employment type. Two principals whose
entitlements are identical share entries; two whose entitlements differ in any respect the
policy reads can never collide, because the difference is in the key.

Note what is deliberately *absent* from the fingerprint: `department` (no policy reads it,
so including it would fragment the cache for nothing) and `valid_until` (expiry is enforced
live by the re-fetch in decision 1, and folding a timestamp into a cache key defeats the
cache).

**3. A visibility epoch, bumped explicitly.** An entry cached before a document was
restricted must not outlive the change. Every operation that alters what anyone can see —
loading the corpus, reapplying ACLs, changing policies — bumps a counter that is part of
the key, retiring the whole cache. Deriving the epoch from timestamps was considered and
rejected: `index reacl` rewrites chunk ACLs without touching any `updated_at`, so a derived
epoch would have missed the single most likely reason for a visibility change.

The cost of the semantic part — matching *similar* queries rather than identical ones — is
that a near miss serves an answer to a question nobody asked, and does so silently. The
threshold is therefore tight (0.97 cosine) and the stored query text is returned alongside
the hit so a caller can log what it actually answered.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import delete, func, select, update

from gatekeeper.core.db import admin_session
from gatekeeper.core.models import CacheEpoch, QueryCacheEntry

if TYPE_CHECKING:
    from gatekeeper.core.principal import Principal
    from gatekeeper.llm.embeddings import Embedder

# Tight on purpose. At 0.90 the cache starts answering "what is the parental leave policy"
# with results retrieved for "what is the parental leave policy in the Netherlands" --
# plausible, wrong, and invisible.
SIMILARITY_THRESHOLD = 0.97


def entitlement_fingerprint(principal: Principal, epoch: int) -> str:
    """Hash of every attribute the access policy consults, plus the visibility epoch.

    Kept deliberately in step with `gatekeeper.authorize()`. If a new attribute becomes
    load-bearing in the policy and is not added here, two principals who differ only in
    that attribute would share cache entries — which is the leak this function exists to
    prevent. `test_fingerprint_covers_every_claim_the_policy_reads` pins the set.
    """
    payload = json.dumps(
        {
            "epoch": epoch,
            "tenant": str(principal.tenant_id),
            "clearance": int(principal.clearance),
            "groups": sorted(principal.groups),
            "need_to_know": sorted(principal.need_to_know),
            "region": principal.region,
            "employment_type": principal.employment_type,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def current_epoch() -> int:
    async with admin_session() as session:
        value = (await session.execute(select(func.max(CacheEpoch.epoch)))).scalar_one_or_none()
    return int(value or 0)


async def bump_epoch(reason: str) -> int:
    """Retire the entire cache. Called by anything that changes what anyone can see."""
    async with admin_session() as session:
        current = (await session.execute(select(func.max(CacheEpoch.epoch)))).scalar_one_or_none()
        nxt = int(current or 0) + 1
        session.add(CacheEpoch(epoch=nxt, reason=reason))
    return nxt


@dataclass
class CacheHit:
    chunk_ids: list[UUID]
    cached_query: str
    similarity: float


async def lookup(
    principal: Principal, query_vector: list[float], embedder: Embedder, epoch: int
) -> CacheHit | None:
    """Nearest cached query within this principal's entitlement bucket."""
    fingerprint = entitlement_fingerprint(principal, epoch)
    column = QueryCacheEntry.embedding_384
    distance = column.cosine_distance(query_vector)

    async with admin_session() as session:
        row = (
            await session.execute(
                select(
                    QueryCacheEntry.id,
                    QueryCacheEntry.query_text,
                    QueryCacheEntry.chunk_ids,
                    distance.label("d"),
                )
                .where(
                    QueryCacheEntry.fingerprint == fingerprint,
                    QueryCacheEntry.embedding_model == embedder.space.model,
                )
                .order_by(distance)
                .limit(1)
            )
        ).first()

        if row is None or (1.0 - float(row.d)) < SIMILARITY_THRESHOLD:
            return None

        await session.execute(
            update(QueryCacheEntry)
            .where(QueryCacheEntry.id == row.id)
            .values(hits=QueryCacheEntry.hits + 1, last_hit_at=func.now())
        )
        return CacheHit(
            chunk_ids=list(row.chunk_ids),
            cached_query=row.query_text,
            similarity=1.0 - float(row.d),
        )


async def store(
    principal: Principal,
    query: str,
    query_vector: list[float],
    chunk_ids: list[UUID],
    embedder: Embedder,
    epoch: int,
) -> None:
    fingerprint = entitlement_fingerprint(principal, epoch)
    async with admin_session() as session:
        entry = QueryCacheEntry(
            fingerprint=fingerprint,
            epoch=epoch,
            query_text=query,
            chunk_ids=chunk_ids,
            embedding_model=embedder.space.model,
        )
        entry.embedding_384 = query_vector
        session.add(entry)


async def purge(keep_current: bool = True) -> int:
    """Delete entries from retired epochs.

    Correctness does not depend on this running: the epoch is inside the fingerprint hash,
    so a retired entry is already unreachable. `epoch` is stored as its own column purely
    so that "unreachable" is also *visible* — a hash tells you nothing about which epoch it
    belongs to, which made the first version of this function unable to do its job.
    """
    epoch = await current_epoch()
    async with admin_session() as session:
        stmt = delete(QueryCacheEntry)
        if keep_current:
            stmt = stmt.where(QueryCacheEntry.epoch < epoch)
        result = await session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)
