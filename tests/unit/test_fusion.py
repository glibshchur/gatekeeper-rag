"""Reciprocal Rank Fusion properties."""

from __future__ import annotations

from gatekeeper.retrieval.fusion import reciprocal_rank_fusion
from gatekeeper.retrieval.search import RetrievedChunk


def chunk(cid: str, score: float = 0.0) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        document_id=f"d-{cid}",
        title=cid,
        path=f"{cid}.md",
        source_uri=None,
        heading_path=[cid],
        content=cid,
        score=score,
        sensitivity="internal",
    )


def ids(chunks: list[RetrievedChunk]) -> list[str]:
    return [c.chunk_id for c in chunks]


def test_a_single_ranking_is_returned_in_order() -> None:
    ranking = [chunk("a"), chunk("b"), chunk("c")]
    assert ids(reciprocal_rank_fusion([ranking])) == ["a", "b", "c"]


def test_agreement_between_rankings_outranks_a_single_first_place() -> None:
    """The property RRF exists for: a document both retrievers like beats one that only
    one of them put first. Agreement between methods that fail differently is evidence."""
    dense = [chunk("solo"), chunk("both"), chunk("x")]
    lexical = [chunk("y"), chunk("both"), chunk("z")]
    fused = ids(reciprocal_rank_fusion([dense, lexical]))
    assert fused[0] == "both"
    assert fused.index("both") < fused.index("solo")


def test_scores_are_replaced_by_fused_scores() -> None:
    # Cosine similarity and ts_rank_cd are on incomparable scales; carrying either
    # through would make the output's scores meaningless.
    fused = reciprocal_rank_fusion([[chunk("a", score=0.99)]], rrf_k=60)
    assert fused[0].score == 1.0 / 61


def test_inputs_are_not_mutated() -> None:
    """An ablation reuses candidate lists across arms. Mutating them in place would leak
    one configuration's scores into the next one's inputs."""
    original = chunk("a", score=0.42)
    reciprocal_rank_fusion([[original]])
    assert original.score == 0.42


def test_larger_rrf_k_flattens_the_influence_of_top_ranks() -> None:
    first, second = chunk("first"), chunk("second")
    tight = reciprocal_rank_fusion([[first, second]], rrf_k=1)
    flat = reciprocal_rank_fusion([[first, second]], rrf_k=1000)
    tight_gap = tight[0].score - tight[1].score
    flat_gap = flat[0].score - flat[1].score
    assert flat_gap < tight_gap


def test_ties_are_broken_deterministically() -> None:
    # Without a tiebreak, equal-scoring chunks reorder between runs and an ablation
    # measures dictionary ordering instead of retrieval.
    a = ids(reciprocal_rank_fusion([[chunk("b")], [chunk("a")]]))
    b = ids(reciprocal_rank_fusion([[chunk("a")], [chunk("b")]]))
    assert a == b == ["a", "b"]


def test_limit_truncates_after_fusion_not_before() -> None:
    dense = [chunk("x"), chunk("y"), chunk("shared")]
    lexical = [chunk("shared"), chunk("z")]
    # `shared` ranks 3rd and 1st; truncating inputs to 1 would drop it entirely.
    assert ids(reciprocal_rank_fusion([dense, lexical], limit=1)) == ["shared"]


def test_empty_input_yields_nothing() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []
