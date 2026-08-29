"""Lexical retrieval — the half of hybrid search that dense embeddings are bad at.

Embeddings are good at meaning and poor at tokens. "SEC-4417", "IRS Form 1099",
"gitlab-org/gitlab#12345", "25 USD" have no useful neighbourhood in embedding space, and
a policy corpus is largely made of exactly those: thresholds, form numbers, entity names,
ticket references. Those are the queries lexical search answers perfectly and dense
search answers with plausible nonsense.

Runs under the same RLS policy as the dense path, on the same relation, so authorization
is identical across both halves of the fusion. That is not incidental: if the two halves
filtered differently, fusing them would be a way to launder access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select, text

from gatekeeper.core.models import Chunk, Document
from gatekeeper.retrieval.search import RetrievedChunk, coarse_predicate

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from gatekeeper.core.principal import Principal


# Postgres has no built-in "any of these terms" query builder: `plainto_tsquery` and
# `websearch_to_tsquery` both AND the terms together. That is right for a search box and
# badly wrong for a question. "What are the rules for expensing meals and travel on a
# business trip?" becomes `rule & expens & meal & travel & business & trip`, which
# demands all six lexemes in one chunk and matches almost nothing — measured at nDCG@10
# 0.29 and recall 0.32 before this was fixed.
#
# Instead: run the query through `to_tsvector` to get stemmed, stop-word-filtered
# lexemes, then OR them. `ts_rank_cd` still rewards chunks matching more of them, which
# is the behaviour a question needs. The `nullif` guards a query that is entirely
# stop-words, where the aggregate is empty and `''::tsquery` would raise.
_OR_TSQUERY = """
    (SELECT coalesce(nullif(string_agg(lexeme, ' | '), ''), '')::tsquery
     FROM unnest(to_tsvector('english', :lexical_query)))
"""


async def lexical_topk(
    session: AsyncSession,
    query: str,
    k: int = 50,
    tenant_id: UUID | None = None,
    principal: Principal | None = None,
) -> list[RetrievedChunk]:
    """Top-k by `ts_rank_cd` over an OR of the query's lexemes.

    `tenant_id` is a performance predicate, not a security one, for the same reason as in
    the dense path: it is btree-indexable and the ABAC predicate is not, so it lets the
    planner bound the candidate set before ranking it.
    """
    tsquery = text(_OR_TSQUERY).bindparams(lexical_query=query)
    score = func.ts_rank_cd(Chunk.content_tsv, tsquery)

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
            score.label("score"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.content_tsv.op("@@")(tsquery))
        .order_by(score.desc())
        .limit(k)
    )
    if tenant_id is not None:
        stmt = stmt.where(Chunk.tenant_id == tenant_id)
    if principal is not None:
        # Matters more here than on the dense path: lexical search hands the policy every
        # row matching the tsquery, so narrowing the candidate set before ranking is the
        # difference between ranking thousands of rows and ranking hundreds.
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
            score=float(row.score),
            sensitivity=row.sensitivity,
        )
        for row in rows
    ]
