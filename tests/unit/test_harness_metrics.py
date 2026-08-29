"""Metric arithmetic. If these are wrong, every number in the ablation is wrong."""

from __future__ import annotations

import math

from gatekeeper.evals.harness import GoldenQuestion, QuestionResult


def result(retrieved: list[str], relevant: tuple[str, ...]) -> QuestionResult:
    question = GoldenQuestion(id="q", question="?", relevant=relevant, asker="raj", category="test")
    return QuestionResult(question=question, retrieved=retrieved, latency_ms=0)


def test_hit_is_any_overlap() -> None:
    assert result(["a", "b"], ("b",)).hit
    assert not result(["a", "b"], ("c",)).hit


def test_recall_is_the_labelled_fraction_found() -> None:
    assert result(["a", "x"], ("a", "b")).recall() == 0.5
    assert result(["a", "b"], ("a", "b")).recall() == 1.0
    assert result(["x"], ("a",)).recall() == 0.0


def test_reciprocal_rank_uses_the_first_relevant_position() -> None:
    assert result(["x", "a"], ("a",)).reciprocal_rank() == 0.5
    assert result(["a"], ("a",)).reciprocal_rank() == 1.0
    assert result(["x", "y"], ("a",)).reciprocal_rank() == 0.0


def test_ndcg_is_one_when_the_labelled_documents_lead() -> None:
    assert result(["a", "b", "x"], ("a", "b")).ndcg() == 1.0


def test_ndcg_discounts_by_position() -> None:
    top = result(["a", "x", "y"], ("a",)).ndcg()
    third = result(["x", "y", "a"], ("a",)).ndcg()
    assert top == 1.0
    assert math.isclose(third, 1.0 / math.log2(4))
    assert third < top


def test_ndcg_ideal_accounts_for_more_labels_than_k() -> None:
    # Three labelled documents but k=2: perfect ordering must still score 1.0, or a
    # question with many relevant documents is permanently penalised.
    assert result(["a", "b"], ("a", "b", "c")).ndcg(k=2) == 1.0


def test_ndcg_is_zero_with_no_relevant_results() -> None:
    assert result(["x", "y"], ("a",)).ndcg() == 0.0


def test_empty_retrieval_scores_zero_everywhere() -> None:
    empty = result([], ("a",))
    assert (empty.recall(), empty.reciprocal_rank(), empty.ndcg()) == (0.0, 0.0, 0.0)
    assert not empty.hit
