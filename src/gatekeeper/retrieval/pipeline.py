"""The retrieval pipeline, described by a config object rather than by code paths.

Every stage this project can run — dense, lexical, fusion, reranking — is a field on
:class:`RetrievalConfig`. The ablation study in `gatekeeper.evals.ablation` is then a list
of configs rather than a set of branches, which matters for a reason beyond tidiness: a
comparison whose arms are separate code paths is a comparison of two implementations, and
any difference between them is a candidate explanation for the result. Here the arms
differ only in the data that describes them.

Authorization is unchanged by any of it. Both retrievers run under the same RLS policy on
the same relation, and reranking only reorders rows the database already returned. No
stage can widen what a principal sees; the red-team suite asserts that against the
composed pipeline, not just the dense path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID

from gatekeeper.core import audit as audit_log
from gatekeeper.core.db import admin_session, principal_session
from gatekeeper.retrieval import cache as query_cache
from gatekeeper.retrieval.fusion import DEFAULT_RRF_K, reciprocal_rank_fusion
from gatekeeper.retrieval.lexical import lexical_topk
from gatekeeper.retrieval.search import (
    DEFAULT_EF_SEARCH,
    RetrievedChunk,
    SearchResult,
    _ann_query,
    fetch_by_ids,
)

if TYPE_CHECKING:
    from gatekeeper.core.principal import Principal
    from gatekeeper.llm.embeddings import Embedder
    from gatekeeper.llm.rerank import CrossEncoderReranker


@dataclass(frozen=True)
class RetrievalConfig:
    """One arm of the ablation, and the shape of a production configuration."""

    name: str
    dense: bool = True
    lexical: bool = False
    rerank: bool = False
    # Candidates each retriever proposes before fusion and reranking.
    #
    # 20, from the sweep in docs/ABLATION.md, not from intuition. Intuition says a bigger
    # pool can only help the reranker; the measurement says otherwise. Going from 20 to 50
    # costs 2x latency *and* loses nDCG (0.797 -> 0.784), because past the first ~20 the
    # pool is mostly irrelevant candidates and the cross-encoder occasionally promotes
    # one. Pool 10 is faster still and clearly worse (0.768), so the knee is real.
    candidates: int = 20
    rerank_candidates: int = 20
    rrf_k: int = DEFAULT_RRF_K
    ef_search: int = DEFAULT_EF_SEARCH

    def __post_init__(self) -> None:
        if not (self.dense or self.lexical):
            raise ValueError(f"config {self.name!r} retrieves nothing")

    @property
    def description(self) -> str:
        parts = []
        if self.dense:
            parts.append("dense")
        if self.lexical:
            parts.append("lexical")
        joined = " + ".join(parts)
        if self.dense and self.lexical:
            joined += f" (RRF k={self.rrf_k})"
        if self.rerank:
            joined += " → cross-encoder"
        return joined


BASELINE = RetrievalConfig(name="dense", dense=True)
LEXICAL_ONLY = RetrievalConfig(name="lexical", dense=False, lexical=True)
HYBRID = RetrievalConfig(name="hybrid", dense=True, lexical=True)
HYBRID_RERANK = RetrievalConfig(name="hybrid+rerank", dense=True, lexical=True, rerank=True)
DENSE_RERANK = RetrievalConfig(name="dense+rerank", dense=True, rerank=True)

ABLATION_ARMS = (BASELINE, LEXICAL_ONLY, HYBRID, DENSE_RERANK, HYBRID_RERANK)

# The production default, chosen from docs/ABLATION.md rather than from the assumption
# that more stages is better. `hybrid+rerank` and `dense+rerank` score the same to within
# run-to-run noise (0.797 vs 0.796 nDCG@10) and hybrid costs 121 ms more per query for it.
# The lexical arm stays implemented and stays in the ablation -- the category breakdown
# shows it genuinely helps where dense is weakest, and RRF weighted by arm quality is
# untried -- but shipping it on by default would be paying for a stage that does not yet
# pay back.
DEFAULT_CONFIG = DENSE_RERANK


async def retrieve(
    principal: Principal,
    query: str,
    embedder: Embedder,
    *,
    config: RetrievalConfig = DEFAULT_CONFIG,
    k: int = 10,
    reranker: CrossEncoderReranker | None = None,
    count_withheld: bool = False,
    audit: bool = True,
    use_cache: bool = False,
) -> SearchResult:
    """Run the configured pipeline as `principal`.

    `reranker` is injected rather than constructed here: loading the cross-encoder takes
    seconds, and an ablation that reloaded it per arm would spend most of its time in
    model initialisation and report the difference as latency.
    """
    if config.rerank and reranker is None:
        raise ValueError(f"config {config.name!r} needs a reranker, none was provided")

    started = time.monotonic()
    query_vector = embedder.encode_query(query).tolist() if config.dense else None

    # The cache is off by default. It is safe -- a hit is re-authorized by `fetch_by_ids`
    # -- but a semantic cache can answer a *similar* question rather than the one asked,
    # and that is a correctness trade a caller should opt into rather than inherit.
    if use_cache and query_vector is not None:
        epoch = await query_cache.current_epoch()
        hit = await query_cache.lookup(principal, query_vector, embedder, epoch)
        if hit is not None:
            async with principal_session(principal) as session:
                chunks = await fetch_by_ids(session, hit.chunk_ids, principal)
                latency_ms = int((time.monotonic() - started) * 1000)
                if audit:
                    await audit_log.append(
                        session,
                        principal,
                        action=f"retrieve:{config.name}:cached",
                        query_text=query,
                        retrieved=sorted({UUID(c.document_id) for c in chunks}),
                        latency_ms=latency_ms,
                    )
            return SearchResult(
                query=query,
                chunks=chunks,
                latency_ms=latency_ms,
                cached=True,
                cached_query=hit.cached_query,
            )

    unfiltered: list[RetrievedChunk] = []
    if count_withheld and query_vector is not None:
        async with admin_session() as session:
            # Deliberately NOT passed `principal`: this is the baseline the
            # withheld count is measured against, so it must stay genuinely
            # unfiltered. Applying `coarse_predicate` here would remove exactly the
            # rows the policy is about to deny, and the count would silently under-
            # report every denial caused by group or clearance — reporting 0 withheld
            # while withholding plenty, which is worse than not reporting at all.
            unfiltered = await _ann_query(
                session,
                embedder=embedder,
                query_vector=query_vector,
                k=k,
                ef_search=config.ef_search,
                tenant_id=principal.tenant_id,
            )

    async with principal_session(principal) as session:
        rankings: list[list[RetrievedChunk]] = []
        if query_vector is not None:
            rankings.append(
                await _ann_query(
                    session,
                    embedder=embedder,
                    query_vector=query_vector,
                    k=config.candidates,
                    ef_search=config.ef_search,
                    tenant_id=principal.tenant_id,
                    principal=principal,
                )
            )
        if config.lexical:
            rankings.append(
                await lexical_topk(
                    session,
                    query,
                    k=config.candidates,
                    tenant_id=principal.tenant_id,
                    principal=principal,
                )
            )

        if len(rankings) > 1:
            candidates = reciprocal_rank_fusion(rankings, rrf_k=config.rrf_k)
        else:
            candidates = list(rankings[0])

        if config.rerank and reranker is not None:
            candidates = reranker.rerank(query, candidates[: config.rerank_candidates], top_k=k)

        chunks = candidates[:k]

        withheld: int | None = None
        denied_docs: list[UUID] = []
        if count_withheld and unfiltered:
            visible = {c.chunk_id for c in chunks}
            blocked = [c for c in unfiltered if c.chunk_id not in visible]
            withheld = len(blocked)
            denied_docs = sorted({UUID(c.document_id) for c in blocked})

        latency_ms = int((time.monotonic() - started) * 1000)

        if audit:
            await audit_log.append(
                session,
                principal,
                action=f"retrieve:{config.name}",
                query_text=query,
                retrieved=sorted({UUID(c.document_id) for c in chunks}),
                denied=denied_docs,
                latency_ms=latency_ms,
            )

    if use_cache and query_vector is not None:
        await query_cache.store(
            principal,
            query,
            query_vector,
            [UUID(c.chunk_id) for c in chunks],
            embedder,
            await query_cache.current_epoch(),
        )

    return SearchResult(query=query, chunks=chunks, latency_ms=latency_ms, withheld=withheld)


def with_candidates(config: RetrievalConfig, candidates: int) -> RetrievalConfig:
    return replace(config, candidates=candidates, rerank_candidates=candidates)
