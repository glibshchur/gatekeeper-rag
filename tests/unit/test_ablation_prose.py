"""The generated findings must not be able to drift from the table above them.

These exist because two versions of this prose were wrong in ways nobody would catch by
reading the code: a hand-written paragraph that went stale against its own table
(nDCG 0.708 in the text, 0.699 in the table), and a computed one that anchored on
whichever pool scored highest in a given run and produced "pool 50 is within noise of
pool 50", announcing the wrong default.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from gatekeeper.evals import ablation as ab
from gatekeeper.retrieval.pipeline import DEFAULT_CONFIG


@dataclass
class _Cfg:
    candidates: int
    name: str = "pool"
    rerank_candidates: int = 0
    description: str = ""


@dataclass
class _Report:
    config: _Cfg
    ndcg: float
    mrr: float = 0.0
    p50_ms: float = 100.0
    p95_ms: float = 120.0

    def by_category(self) -> dict[str, float]:
        return {}


def _sweep(pairs: list[tuple[int, float, float]]) -> ab.Ablation:
    return ab.Ablation(
        reports=[],
        sweep=[_Report(_Cfg(n), ndcg, p50_ms=ms) for n, ndcg, ms in pairs],  # type: ignore[list-item]
        k=10,
        corpus_chunks=73801,
        question_count=58,
    )


def test_the_claim_is_anchored_on_the_configured_default_not_the_run_winner() -> None:
    """The regression. When the largest pool ties the default, an earlier version compared
    the winner to itself and declared it the default."""
    text = ab._pool_claim(_sweep([(10, 0.761, 233), (20, 0.791, 344), (50, 0.791, 675)]))
    assert "pool 50 is within noise of pool 50" not in text
    assert "indistinguishable from the configured pool 20" in text
    assert "20 stays the default" in text


def test_a_real_win_for_the_default_is_stated_as_one() -> None:
    text = ab._pool_claim(_sweep([(10, 0.760, 200), (20, 0.800, 300), (50, 0.770, 600)]))
    assert "beats pool 50 outright" in text


def test_a_genuinely_better_large_pool_says_the_default_needs_review() -> None:
    """The claim must be able to contradict the shipped configuration. A generator that
    can only produce agreement is decoration, not measurement."""
    text = ab._pool_claim(_sweep([(10, 0.700, 200), (20, 0.760, 300), (50, 0.820, 600)]))
    assert "a real gain" in text
    assert "due a review" in text


def test_latency_multiple_and_below_knee_loss_are_reported() -> None:
    text = ab._pool_claim(_sweep([(10, 0.761, 233), (20, 0.791, 344), (50, 0.791, 675)]))
    assert "2.0x the latency" in text
    assert "pool 10 scores 0.761" in text


def test_an_empty_sweep_says_so_rather_than_inventing_a_claim() -> None:
    assert "no sweep" in ab._pool_claim(_sweep([]))


def test_a_sweep_that_does_not_bracket_the_default_is_refused() -> None:
    assert "does not bracket" in ab._pool_claim(_sweep([(20, 0.79, 300)]))


@pytest.mark.parametrize("default_n", [DEFAULT_CONFIG.rerank_candidates])
def test_the_anchor_tracks_the_shipped_config(default_n: int) -> None:
    """If someone changes the default pool size, this prose must follow it."""
    text = ab._pool_claim(_sweep([(10, 0.70, 200), (default_n, 0.79, 300), (99, 0.79, 900)]))
    assert f"configured pool {default_n}" in text
