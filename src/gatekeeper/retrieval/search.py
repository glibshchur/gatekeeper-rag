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
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, text

from gatekeeper.core.db import admin_session, principal_session
from gatekeeper.core.models import Chunk, Document

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from gatekeeper.core.principal import Principal
    from gatekeeper.llm.embeddings import Embedder


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


async def _ann_query(
    session: AsyncSession,
    *,
    embedder: Embedder,
    query_vector: list[float],
    k: int,
    ef_search: int,
    tenant_id: UUID | None = None,
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
    # Only needed on the admin plane, where no RLS policy is scoping the query for us.
    if tenant_id is not None:
        stmt = stmt.where(Chunk.tenant_id == tenant_id)
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


async def search(
    principal: Principal,
    query: str,
    embedder: Embedder,
    *,
    k: int = 10,
    ef_search: int = 100,
    count_withheld: bool = False,
) -> SearchResult:
    started = time.monotonic()
    query_vector = embedder.encode_query(query).tolist()

    async with principal_session(principal) as session:
        chunks = await _ann_query(
            session, embedder=embedder, query_vector=query_vector, k=k, ef_search=ef_search
        )

    withheld: int | None = None
    if count_withheld:
        # Scoped to the principal's own tenant. Comparing against the whole database
        # would count other tenants' chunks as "withheld", which turns a transparency
        # feature into a side channel disclosing that other corpora exist at all.
        async with admin_session() as session:
            unfiltered = await _ann_query(
                session,
                embedder=embedder,
                query_vector=query_vector,
                k=k,
                ef_search=ef_search,
                tenant_id=principal.tenant_id,
            )
        visible_ids = {c.chunk_id for c in chunks}
        withheld = sum(1 for c in unfiltered if c.chunk_id not in visible_ids)

    return SearchResult(
        query=query,
        chunks=chunks,
        latency_ms=int((time.monotonic() - started) * 1000),
        withheld=withheld,
    )
