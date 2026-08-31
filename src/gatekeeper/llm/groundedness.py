"""Check that an answer's claims are actually supported by the sources it cites.

Citation markers are cheap to produce and prove nothing. A model can write "[2]" after a
sentence that source 2 does not support, and every citation-checking mechanism that only
counts brackets will call the answer grounded. This splits the answer into sentences and
asks, per sentence, whether any cited chunk actually contains it.

**Two layers, because measuring the first one showed exactly where it fails.** Embedding
similarity alone was tested against deliberate corruptions of a source sentence:

| corruption | similarity | embeddings alone |
|---|---:|---|
| faithful restatement | 0.884 | supported (correct) |
| negation — "receipts are **not** required" | 0.673 | flagged (correct) |
| **75 USD → 750 USD** | 0.867 | **supported — wrong** |
| **75 USD → 7 USD** | 0.872 | **supported — wrong** |
| **30 days → 300 days** | 0.775 | **supported — wrong** |
| reordered causality | 0.512 | flagged (correct) |
| outright fabrication | 0.544 | flagged (correct) |

Negation *is* caught, which was the opposite of the expectation this module was written
with. The real blind spot is **numbers**: changing a limit by a factor of ten scores 0.867
against a faithful 0.884 — statistically indistinguishable. In a policy corpus that is the
worst possible place to be blind, because the content *is* thresholds, limits and
deadlines, and a confidently wrong expense limit is the exact failure a citation is
supposed to prevent.

So the second layer is not another model: it is an exact check. Every number in a claim
must appear in the source that supposedly supports it. Cheap, no false negatives on the
case that matters, and it closes precisely the gap similarity cannot see.

What remains uncaught is semantic contradiction that changes no number and negates no
verb — "managers approve expenses" versus "directors approve expenses". That needs
entailment, and the per-sentence score with its best-matching source is reported rather
than a bare verdict so a reviewer can check.

Sentences that make no factual claim — "Here is a summary.", "I hope that helps." — would
otherwise be reported as unsupported forever, so trivially short and citation-free
fragments are excluded rather than counted against the answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from gatekeeper.ingest.chunking import split_sentences


def split_answer(answer: str) -> list[str]:
    """Split a generated answer into claims.

    The chunker's prose splitter is not enough here: a model answers in Markdown, and a
    bulleted list is one "sentence" to a splitter that only looks for terminal punctuation.
    That produced claims spanning four bullets, each scored against a single source and
    each unsupported for reasons that had nothing to do with grounding.

    Lines first, then sentences within a line.
    """
    claims: list[str] = []
    for line in answer.splitlines():
        stripped = line.strip().lstrip("-*+ ").strip()
        if not stripped:
            continue
        claims.extend(split_sentences(stripped) or [stripped])
    return claims


if TYPE_CHECKING:
    from gatekeeper.llm.embeddings import Embedder
    from gatekeeper.retrieval.search import RetrievedChunk

# Cosine similarity between a claim and the passage that supports it. Calibrated on this
# corpus: a sentence lifted from a chunk scores above 0.9, a paraphrase 0.75-0.9, and an
# unrelated fabrication below 0.6. 0.72 sits in the gap and is deliberately generous --
# a false "unsupported" trains readers to ignore the warning.
SUPPORT_THRESHOLD = 0.72

# Below this many words a sentence is treated as discourse rather than a claim.
MIN_CLAIM_WORDS = 5

# Digits only: "75", "1,200", "3.5". Ordinals and spelled-out numbers are out of scope --
# a policy states limits in figures.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Citation markers are numbers the model wrote *about* the sources, not claims about the
# world. The first real generated answer scored 0% supported entirely because "[2]" and
# "[3]" were being read as unsupported figures -- a bug no amount of hand-written test
# answers surfaced, because hand-written answers do not carry citations.
_CITATION = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")

# Markdown scaffolding in a generated answer: bullets, bold, headings. Left in place for
# similarity (it is noise, not signal) but stripped before numeric extraction so "**$120**"
# and "120" compare equal.
_MARKUP = re.compile(r"[*_`#>]+")


def numbers_in(text: str, *, strip_citations: bool = False) -> set[str]:
    """Numeric tokens, normalised so "1,200" and "1200" compare equal.

    `strip_citations` is set for claims and not for sources: a source has no citation
    markers, and stripping bracketed digits from one would silently discard a real figure
    written as "[2] business days".
    """
    if strip_citations:
        text = _CITATION.sub(" ", text)
    text = _MARKUP.sub(" ", text)
    return {m.group(0).replace(",", "").rstrip(".") for m in _NUMBER.finditer(text)}


@dataclass
class SentenceSupport:
    sentence: str
    score: float
    best_source: int | None
    """1-based index into the sources, matching the citation markers the model writes."""
    unsupported_numbers: tuple[str, ...] = field(default_factory=tuple)
    """Figures in the claim that appear in no source. A single one is disqualifying."""

    @property
    def supported(self) -> bool:
        # A number the sources do not contain is decisive regardless of similarity: it is
        # the one corruption embeddings cannot see, and in a policy corpus it is the one
        # that matters most.
        return self.score >= SUPPORT_THRESHOLD and not self.unsupported_numbers


@dataclass
class GroundednessReport:
    sentences: list[SentenceSupport]
    skipped: int = 0

    @property
    def unsupported(self) -> list[SentenceSupport]:
        return [s for s in self.sentences if not s.supported]

    @property
    def score(self) -> float:
        """Fraction of claim-bearing sentences with a supporting source."""
        if not self.sentences:
            return 1.0
        return sum(s.supported for s in self.sentences) / len(self.sentences)

    @property
    def grounded(self) -> bool:
        return not self.unsupported


def verify(answer: str, sources: list[RetrievedChunk], embedder: Embedder) -> GroundednessReport:
    """Score each claim in `answer` against the passages in `sources`."""
    if not sources:
        # Nothing to be grounded in. Reporting 1.0 here would be the wrong kind of
        # generous: an answer with no sources is unverifiable, not verified.
        return GroundednessReport(sentences=[], skipped=0)

    candidates = split_answer(answer)
    claims = [s for s in candidates if len(s.split()) >= MIN_CLAIM_WORDS]
    if not claims:
        return GroundednessReport(sentences=[], skipped=len(candidates))

    source_vectors = embedder.encode_passages([c.content for c in sources])
    claim_vectors = embedder.encode_passages(claims)

    # Vectors are L2-normalised by every backend, so the dot product is cosine similarity.
    similarity = claim_vectors @ source_vectors.T

    # Checked against every source, not only the best-matching one: a claim may
    # legitimately combine a figure from one passage with context from another.
    available = set().union(*(numbers_in(c.content) for c in sources))

    supports = []
    for row, sentence in zip(similarity, claims, strict=True):
        best = int(np.argmax(row))
        supports.append(
            SentenceSupport(
                sentence=sentence,
                score=round(float(row[best]), 4),
                best_source=best + 1,
                unsupported_numbers=tuple(
                    sorted(numbers_in(sentence, strip_citations=True) - available)
                ),
            )
        )
    return GroundednessReport(sentences=supports, skipped=len(candidates) - len(claims))


def annotate(report: GroundednessReport) -> str:
    """One line per unsupported claim, for a CLI or a log."""
    if not report.sentences:
        return "no verifiable claims"
    if report.grounded:
        return f"grounded: {len(report.sentences)} of {len(report.sentences)} claims supported"
    lines = [
        f"{report.score:.0%} of {len(report.sentences)} claims supported; "
        f"{len(report.unsupported)} unsupported:"
    ]
    for s in report.unsupported:
        why = (
            f"figures {list(s.unsupported_numbers)} appear in no source"
            if s.unsupported_numbers
            else f"similarity {s.score:.2f}"
        )
        lines.append(f"  [{why}, best match source {s.best_source}] {s.sentence[:100]}")
    return "\n".join(lines)
