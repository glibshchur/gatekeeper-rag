"""Groundedness verification.

Uses the real local embedder, so these are slower than the rest of the unit suite but test
the thing that actually ships rather than a stub of it. The most important test is the last
one: it pins the limitation, so nobody reads a green suite as "contradictions are caught".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from gatekeeper.llm.embeddings import Embedder, LocalOnnxEmbedder
from gatekeeper.llm.groundedness import SUPPORT_THRESHOLD, annotate, verify
from gatekeeper.retrieval.search import RetrievedChunk

EXPENSES = (
    "Employees may expense meals up to 75 USD per day while travelling for work. "
    "Receipts are required for any expense above 25 USD and must be submitted within "
    "30 days."
)
ONBOARDING = (
    "New team members complete orientation in their first week, meet their onboarding "
    "buddy, and finish the security training module before receiving production access."
)


@pytest.fixture(scope="module")
def embedder() -> Iterator[Embedder]:
    yield LocalOnnxEmbedder()


def source(content: str, name: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=name,
        document_id=name,
        title=name,
        path=f"{name}.md",
        source_uri=None,
        heading_path=[name],
        content=content,
        score=0.9,
        sensitivity="internal",
    )


SOURCES = [source(EXPENSES, "expenses"), source(ONBOARDING, "onboarding")]


def test_a_claim_lifted_from_a_source_is_supported(embedder: Embedder) -> None:
    report = verify(
        "Employees may expense meals up to 75 USD per day while travelling for work.",
        SOURCES,
        embedder,
    )
    assert report.grounded
    assert report.sentences[0].best_source == 1


def test_a_fabricated_claim_is_flagged(embedder: Embedder) -> None:
    report = verify(
        "The company reimburses first-class flights for all staff without prior approval.",
        SOURCES,
        embedder,
    )
    assert not report.grounded
    assert report.unsupported[0].score < SUPPORT_THRESHOLD


def test_the_report_attributes_each_claim_to_its_best_source(embedder: Embedder) -> None:
    report = verify(
        "Receipts are required for any expense above 25 USD. "
        "New team members finish security training before getting production access.",
        SOURCES,
        embedder,
    )
    assert [s.best_source for s in report.sentences] == [1, 2]
    assert report.grounded


def test_a_mixed_answer_scores_between_the_extremes(embedder: Embedder) -> None:
    report = verify(
        "Receipts are required for any expense above 25 USD. "
        "Executive equity grants vest over four years with a one year cliff.",
        SOURCES,
        embedder,
    )
    assert 0.0 < report.score < 1.0
    assert len(report.unsupported) == 1


def test_discourse_sentences_are_skipped_not_counted_against_the_answer(
    embedder: Embedder,
) -> None:
    """ "Here is a summary." makes no claim and would otherwise be permanently unsupported,
    which trains a reader to ignore the warning entirely."""
    report = verify(
        "Here you go. Receipts are required for any expense above 25 USD. I hope that helps.",
        SOURCES,
        embedder,
    )
    assert report.skipped >= 1
    assert report.grounded


def test_an_answer_with_no_sources_is_unverifiable_not_verified(embedder: Embedder) -> None:
    report = verify("Receipts are required above 25 USD.", [], embedder)
    assert report.sentences == []
    assert annotate(report) == "no verifiable claims"


def test_annotate_names_the_source_that_should_have_supported_the_claim(
    embedder: Embedder,
) -> None:
    report = verify(
        "The company reimburses first-class flights for all staff without prior approval.",
        SOURCES,
        embedder,
    )
    text = annotate(report)
    assert "unsupported" in text
    assert "best match source" in text


@pytest.mark.parametrize(
    ("label", "claim", "bad_number"),
    [
        ("ten times the limit", "Employees may expense meals up to 750 USD per day.", "750"),
        ("a tenth of the limit", "Employees may expense meals up to 7 USD per day.", "7"),
        ("ten times the deadline", "Receipts must be submitted within 300 days.", "300"),
    ],
)
def test_a_changed_figure_is_caught_by_the_numeric_layer(
    embedder: Embedder, label: str, claim: str, bad_number: str
) -> None:
    """The failure mode embedding similarity cannot see, and the reason a second layer
    exists at all.

    Measured before the numeric check was added: 75 USD -> 750 USD scored 0.867 against a
    faithful restatement's 0.884 — statistically indistinguishable. In a policy corpus that
    is the worst place to be blind, because the content *is* thresholds and deadlines, and
    a confidently wrong expense limit is exactly what a citation is supposed to prevent.
    """
    report = verify(claim, SOURCES, embedder)
    sentence = report.sentences[0]
    assert sentence.score >= SUPPORT_THRESHOLD, (
        f"{label}: similarity alone would have accepted this, which is the point"
    )
    assert bad_number in sentence.unsupported_numbers
    assert not sentence.supported
    assert not report.grounded


def test_a_figure_present_in_any_source_is_accepted(embedder: Embedder) -> None:
    """Checked against every source, not only the best match: a claim may legitimately
    take a figure from one passage and context from another."""
    report = verify("Receipts are required above 25 USD for travel.", SOURCES, embedder)
    assert report.sentences[0].unsupported_numbers == ()


def test_negation_is_detected_by_similarity(embedder: Embedder) -> None:
    """Recorded because it contradicts the assumption this module was written with. The
    docstring originally claimed contradiction was undetectable; measurement showed
    negation drops similarity to 0.673, comfortably under threshold. Numbers, not
    negation, were the real gap."""
    report = verify("Receipts are not required for any expense above 25 USD.", SOURCES, embedder)
    assert not report.grounded
    assert report.sentences[0].score < SUPPORT_THRESHOLD


def test_substitution_that_changes_no_number_is_still_uncaught(embedder: Embedder) -> None:
    """The remaining honest limit. Swapping one role for another negates no verb and
    changes no figure, so neither layer sees it; that needs entailment. Pinned so a green
    suite is never read as "the verifier catches contradictions"."""
    report = verify(
        "New team members finish the security training module after receiving production access.",
        SOURCES,
        embedder,
    )
    assert report.grounded, (
        "if this now fails, semantic contradiction detection has improved -- update the "
        "docstring in groundedness.py, which tells readers it cannot do this"
    )
