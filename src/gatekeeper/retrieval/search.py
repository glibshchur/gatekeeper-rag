"""Dense retrieval with authorization enforced inside the vector scan.

The whole point of denormalising the effective ACL onto ``chunks`` in Phase 0 is visible
here: the RLS policy and the HNSW scan operate on the same relation, so the authorization
predicate is applied by the same query that ranks by similarity. There is no moment at
which an unauthorized chunk exists in application memory.

The join to ``documents`` is a second, independent check rather than a convenience. Both
relations carry RLS policies; if a chunk's denormalised ACL ever disagreed with its
document's, the inner join drops the row. That fails in the safe direction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import or_, select, text

from gatekeeper.core import audit as audit_log
from gatekeeper.core.db import admin_session, principal_session
from gatekeeper.core.models import Chunk, Document

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from gatekeeper.core.principal import Principal
    from gatekeeper.llm.embeddings import Embedder


# Chosen from docs/BENCHMARKS.md rather than by feel: recall@10 against exact ground
# truth is 0.79-0.98 at ef_search 40-100 and 1.000 at 200, for about 1 ms more. Below
# 200 the results look complete and are quietly missing near neighbours.
DEFAULT_EF_SEARCH = 200


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    title: str
    path: str
    source_uri: str | None
    heading_path: list[str]
    content: str
    score: float
    sensitivity: str

    @property
    def label(self) -> str:
        trail = " > ".join(self.heading_path[1:]) if len(self.heading_path) > 1 else ""
        return f"{self.title}{' — ' + trail if trail else ''}"


@dataclass
class SearchResult:
    query: str
    chunks: list[RetrievedChunk]
    latency_ms: int
    withheld: int | None = None
    """Chunks that would have ranked in the top-k but were removed by authorization.

    Computed only when explicitly requested, because it costs a second query on the admin
    plane. It exists for the audit log and the demo: "8 results, 3 withheld" is the
    observable evidence that the access model did something.
    """


def coarse_predicate(model: type[Chunk] | type[Document], principal: Principal) -> list[Any]:
    """Two of the RLS policy's own clauses, restated in the query.

    This is the fix for the selectivity cliff in ADR 0006. The policy is a function call,
    `gatekeeper.authorize(...)`, and Postgres cannot estimate a function's selectivity —
    it assumes the filter is weak, so it reaches for the HNSW index even when the
    principal can read 5% of the corpus. The graph walk then spends its budget on rows
    the policy will discard.

    The conditions returned here are **not a second access model**. They are literally two
    clauses lifted out of `authorize()`:

        min_clearance <= clearance
        sensitivity = 'public' OR allowed_groups && groups

    Because the policy already requires both, adding them to the query removes nothing the
    policy would have permitted — the coarse predicate is implied by the policy, so it is a
    superset by construction. What it changes is what the planner can *see*: `min_clearance`
    is a small integer column and `allowed_groups` has a GIN index, so selectivity becomes
    estimable and the planner can choose a cheap filtered scan over a small set instead of
    an approximate scan over everything.

    For a principal with no groups the OR collapses to `sensitivity = 'public'` — `&& '{}'`
    is always false — which is also the form that matches the partial HNSW index from
    migration 0008.

    The remaining policy clauses (tenant, expiry, deny rules, need-to-know, jurisdiction)
    stay only in the policy. They are either not usefully indexable or not safe to
    approximate, and restating them would buy nothing.
    """
    conditions: list[Any] = [model.min_clearance <= int(principal.clearance)]
    if principal.groups:
        conditions.append(
            or_(
                model.sensitivity == "public",
                model.allowed_groups.overlap(principal.groups),
            )
        )
    else:
        conditions.append(model.sensitivity == "public")
    return conditions


async def _ann_query(
    session: AsyncSession,
    *,
    embedder: Embedder,
    query_vector: list[float],
    k: int,
    ef_search: int,
    tenant_id: UUID | None = None,
    principal: Principal | None = None,
) -> list[RetrievedChunk]:
    space = embedder.space
    column = getattr(Chunk, space.column)

    # ef_search widens the HNSW candidate list. Under RLS the index returns candidates
    # that policies then remove, so a plain top-k can come back short; iterative_scan
    # makes pgvector keep pulling from the graph until k *visible* rows are found.
    # `relaxed_order` permits slight ordering error in exchange for not scanning the
    # whole graph. Phase 2 measures what that costs in recall.
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))

    distance = column.cosine_distance(query_vector)
    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Document.title,
            Document.path,
            Document.source_uri,
            Chunk.heading_path,
            Chunk.content,
            Chunk.sensitivity,
            distance.label("distance"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(column.is_not(None), Chunk.embedding_model == space.model)
        .order_by(distance)
        .limit(k)
    )
    # Always passed, on both planes, and it is a *performance* predicate rather than a
    # security one -- RLS already restricts the tenant and does so whether or not this
    # clause is present.
    #
    # It exists because the ABAC predicate is opaque to the planner. `authorize()` is a
    # function over columns; Postgres cannot estimate its selectivity, so it assumes the
    # filter is weak and reaches for the HNSW index. When the principal can actually see
    # a tiny fraction of the corpus, the graph walk never encounters their rows and the
    # query returns *fewer results than exist* -- measured at zero results for a
    # principal who could plainly SELECT two matching chunks. `tenant_id = $1` is a
    # btree-indexable predicate the planner does understand, so it can choose an exact
    # scan of a small tenant instead of an approximate scan of a large corpus.
    #
    # If the explicit filter and the policy ever disagreed, the intersection is what
    # comes back, which fails in the safe direction.
    if tenant_id is not None:
        stmt = stmt.where(Chunk.tenant_id == tenant_id)
    if principal is not None:
        stmt = stmt.where(*coarse_predicate(Chunk, principal))
    rows = (await session.execute(stmt)).all()
    return [
        RetrievedChunk(
            chunk_id=str(row.id),
            document_id=str(row.document_id),
            title=row.title,
            path=row.path,
            source_uri=row.source_uri,
            heading_path=list(row.heading_path),
            content=row.content,
            score=1.0 - float(row.distance),
            sensitivity=row.sensitivity,
        )
        for row in rows
    ]


async def unfiltered_candidates(
    principal: Principal,
    query: str,
    embedder: Embedder,
    *,
    k: int = 10,
    ef_search: int = DEFAULT_EF_SEARCH,
) -> list[RetrievedChunk]:
    """The top-k the ranking would return with authorization switched off, within the
    principal's tenant. Used to separate "authorization removed it" from "it ranked
    poorly" -- a distinction the over-block metric is meaningless without."""
    vector = embedder.encode_query(query).tolist()
    async with admin_session() as session:
        return await _ann_query(
            session,
            embedder=embedder,
            query_vector=vector,
            k=k,
            ef_search=ef_search,
            tenant_id=principal.tenant_id,
            principal=principal,
        )


async def search(
    principal: Principal,
    query: str,
    embedder: Embedder,
    *,
    k: int = 10,
    ef_search: int = DEFAULT_EF_SEARCH,
    count_withheld: bool = False,
    audit: bool = True,
) -> SearchResult:
    """Retrieve as `principal`, recording the decision in the audit chain.

    Auditing defaults on. A system whose claim is "the database decides who sees what"
    should be able to show what it decided, and an audit trail that callers opt into is
    an audit trail with holes in it. Pass ``audit=False`` only for benchmarking, where
    the chain's advisory lock would serialise concurrent probes and measure itself.
    """
    started = time.monotonic()
    query_vector = embedder.encode_query(query).tolist()

    # Computed before the principal transaction so the denied ids can be written into
    # the audit entry from inside it. Scoped to the principal's own tenant: comparing
    # against the whole database would count other tenants' chunks as "withheld", which
    # turns a transparency feature into a side channel disclosing other corpora exist.
    unfiltered: list[RetrievedChunk] = []
    if count_withheld:
        async with admin_session() as session:
            unfiltered = await _ann_query(
                session,
                embedder=embedder,
                query_vector=query_vector,
                k=k,
                ef_search=ef_search,
                tenant_id=principal.tenant_id,
            )

    async with principal_session(principal) as session:
        chunks = await _ann_query(
            session,
            embedder=embedder,
            query_vector=query_vector,
            k=k,
            ef_search=ef_search,
            tenant_id=principal.tenant_id,
            principal=principal,
        )

        withheld: int | None = None
        denied_docs: list[UUID] = []
        if count_withheld:
            visible = {c.chunk_id for c in chunks}
            blocked = [c for c in unfiltered if c.chunk_id not in visible]
            withheld = len(blocked)
            denied_docs = sorted({UUID(c.document_id) for c in blocked})

        latency_ms = int((time.monotonic() - started) * 1000)

        if audit:
            # Same transaction as the query: the record and the read it describes commit
            # together, or neither does.
            await audit_log.append(
                session,
                principal,
                action="search",
                query_text=query,
                retrieved=sorted({UUID(c.document_id) for c in chunks}),
                denied=denied_docs,
                latency_ms=latency_ms,
            )

    return SearchResult(
        query=query,
        chunks=chunks,
        latency_ms=latency_ms,
        withheld=withheld,
    )
