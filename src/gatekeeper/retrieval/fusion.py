"""Reciprocal Rank Fusion.

RRF scores a document by the sum of `1 / (rrf_k + rank)` over the rankings it appears in.
Two properties make it the right default here:

* **It consumes ranks, not scores.** Cosine similarity and `ts_rank_cd` are numbers on
  incomparable scales with different distributions; normalising them into a weighted sum
  requires calibration that has to be redone whenever either retriever changes. Ranks are
  already comparable.
* **It has one parameter and is insensitive to it.** `rrf_k=60` is the value from Cormack
  et al. (2009) and the usual default; the constant damps the influence of top ranks so a
  single retriever's confident-but-wrong first hit cannot dominate.

A document ranked modestly by *both* retrievers outscores one ranked first by only one.
That is the whole point: agreement between two methods that fail differently is evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gatekeeper.retrieval.search import RetrievedChunk

DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RetrievedChunk]],
    rrf_k: int = DEFAULT_RRF_K,
    limit: int | None = None,
) -> list[RetrievedChunk]:
    """Fuse ranked lists. Returns chunks carrying their fused score.

    Ties are broken by chunk id so the output is deterministic; without it, equal-scoring
    chunks would reorder between runs and an ablation would measure dictionary ordering.
    """
    scores: dict[str, float] = {}
    seen: dict[str, RetrievedChunk] = {}

    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (rrf_k + rank)
            seen.setdefault(chunk.chunk_id, chunk)

    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    fused = []
    for chunk_id, score in ordered[: limit or len(ordered)]:
        chunk = seen[chunk_id]
        # Replace rather than mutate: the input lists belong to the caller, and an
        # ablation that reuses a candidate list would otherwise see scores from the
        # previous configuration.
        fused.append(
            type(chunk)(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                title=chunk.title,
                path=chunk.path,
                source_uri=chunk.source_uri,
                heading_path=chunk.heading_path,
                content=chunk.content,
                score=score,
                sensitivity=chunk.sensitivity,
            )
        )
    return fused
