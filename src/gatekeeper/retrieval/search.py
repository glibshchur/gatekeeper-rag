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
from gatekeeper.core import telemetry
from gatekeeper.core.db import admin_session, principal_session
from gatekeeper.core.models import Chunk, Document
from gatekeeper.redteam.injection import FLAG_THRESHOLD

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
    injection_score: float = 0.0
    injection_signals: tuple[str, ...] = ()

    @property
    def suspicious(self) -> bool:
        """Flagged by the injection classifier. Annotate it; do not silently drop it."""
        return self.injection_score >= FLAG_THRESHOLD

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
    cached: bool = False
    cached_query: str | None = None
    """The query whose results were reused. Surfaced rather than hidden: a semantic cache
    can answer a question nobody asked, and the only defence is being able to see it."""
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
            Chunk.injection_score,
            Chunk.injection_signals,
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
            injection_score=float(row.injection_score),
            injection_signals=tuple(row.injection_signals),
        )
        for row in rows
    ]


async def fetch_by_ids(
    session: AsyncSession, chunk_ids: list[UUID], principal: Principal
) -> list[RetrievedChunk]:
    """Re-fetch specific chunks, in the given order, through the principal's session.

    This is what makes the query cache safe. A cache hit hands back ids, and those ids go
    through RLS again here -- so an entry that should never have matched this principal
    still cannot produce a row they are not entitled to. The cache is an optimisation on
    *which* rows to ask for, never on whether they are allowed.
    """
    if not chunk_ids:
        return []
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
            Chunk.injection_score,
            Chunk.injection_signals,
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(chunk_ids))
        .where(*coarse_predicate(Chunk, principal))
    )
    rows = {row.id: row for row in (await session.execute(stmt)).all()}
    out: list[RetrievedChunk] = []
    for index, chunk_id in enumerate(chunk_ids):
        row = rows.get(chunk_id)
        if row is None:
            # Either the chunk was deleted or the policy denies it now. Both are normal.
            continue
        out.append(
            RetrievedChunk(
                chunk_id=str(row.id),
                document_id=str(row.document_id),
                title=row.title,
                path=row.path,
                source_uri=row.source_uri,
                heading_path=list(row.heading_path),
                content=row.content,
                # Rank order is preserved from the cached decision; the original scores
                # are not stored, and inventing one would be worse than saying so.
                score=1.0 / (1 + index),
                sensitivity=row.sensitivity,
                injection_score=float(row.injection_score),
                injection_signals=tuple(row.injection_signals),
            )
        )
    return out


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
    poorly" -- a distinction the over-block metric is meaningless without.

    `principal` is used for its tenant and nothing else: passing it to `_ann_query` would
    apply `coarse_predicate` and quietly re-introduce half the policy into the baseline
    this function exists to be free of."""
    vector = embedder.encode_query(query).tolist()
    async with admin_session() as session:
        return await _ann_query(
            session,
            embedder=embedder,
            query_vector=vector,
            k=k,
            ef_search=ef_search,
            tenant_id=principal.tenant_id,
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
    with telemetry.span(
        "search",
        **telemetry.principal_attrs(principal),
        **{
            "retrieval.config": "dense",
            "retrieval.k": k,
            "search.ef_search": ef_search,
            "query.chars": len(query),
            "query.terms": len(query.split()),
        },
    ) as root:
        result = await _search(
            principal,
            query,
            embedder,
            k=k,
            ef_search=ef_search,
            count_withheld=count_withheld,
            audit=audit,
        )
        telemetry.set_attributes(
            root,
            **{
                "retrieval.returned": len(result.chunks),
                "retrieval.withheld": result.withheld,
                "retrieval.latency_ms": result.latency_ms,
            },
        )
        return result


async def _search(
    principal: Principal,
    query: str,
    embedder: Embedder,
    *,
    k: int = 10,
    ef_search: int = DEFAULT_EF_SEARCH,
    count_withheld: bool = False,
    audit: bool = True,
) -> SearchResult:
    started = time.monotonic()
    with telemetry.span("embed.query", **{"embedding.model": embedder.space.model}):
        query_vector = embedder.encode_query(query).tolist()

    # Computed before the principal transaction so the denied ids can be written into
    # the audit entry from inside it. Scoped to the principal's own tenant: comparing
    # against the whole database would count other tenants' chunks as "withheld", which
    # turns a transparency feature into a side channel disclosing other corpora exist.
    unfiltered: list[RetrievedChunk] = []
    if count_withheld:
        # Its own span because it is not free: the first trace showed this unfiltered
        # baseline costing more than the authorized query it is compared against, which
        # is worth knowing before enabling `count_withheld` on a hot path.
        with telemetry.span("search.withheld_baseline"):
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
        with telemetry.span("search.dense", **{"search.ef_search": ef_search}):
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
            with telemetry.span("audit.append"):
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
