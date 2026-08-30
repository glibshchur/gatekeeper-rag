"""Measure the injection classifier on planted attacks and on the real corpus.

Two numbers, and the second is the one that decides whether this ships:

* **Detection rate** over `poison.yaml` — how many planted payloads are caught.
* **False positive rate** over the whole handbook — how much of a real corpus gets
  flagged when, by construction, none of it is an attack.

A classifier with a 95% detection rate and a 3% false positive rate is useless on 74,000
chunks: 2,200 spurious flags is not a review queue, it is a reason to turn the feature
off. The handbook is an unusually hard negative set on purpose — GitLab's security
section discusses red teaming, social engineering and AI safety, so it contains prose
*about* the exact patterns the rules look for.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import yaml
from sqlalchemy import select

from gatekeeper.core.db import admin_session
from gatekeeper.core.models import Chunk, Document
from gatekeeper.redteam.injection import (
    EXEMPLARS,
    FLAG_THRESHOLD,
    QUARANTINE_THRESHOLD,
    RuleScorer,
)

if TYPE_CHECKING:
    from gatekeeper.llm.embeddings import Embedder

POISON_PATH = Path(__file__).parent / "poison.yaml"


@dataclass
class Payload:
    id: str
    category: str
    text: str
    subtle: bool = False


@dataclass
class FalsePositive:
    path: str
    score: float
    signals: tuple[str, ...]
    excerpt: str


@dataclass
class InjectionReport:
    payloads: int = 0
    detected: int = 0
    missed: list[str] = field(default_factory=list)
    by_category: dict[str, tuple[int, int]] = field(default_factory=dict)
    corpus_chunks: int = 0
    flagged: int = 0
    quarantined: int = 0
    worst: list[FalsePositive] = field(default_factory=list)
    semantic_used: bool = False
    # Evidence for the removal of the semantic layer: if these two ranges overlap, no
    # threshold on exemplar similarity can separate evasive payloads from obvious ones.
    subtle_similarity: list[float] = field(default_factory=list)
    plain_similarity: list[float] = field(default_factory=list)

    @property
    def detection_rate(self) -> float:
        return self.detected / self.payloads if self.payloads else 0.0

    @property
    def false_positive_rate(self) -> float:
        return self.flagged / self.corpus_chunks if self.corpus_chunks else 0.0


def load_payloads(path: Path = POISON_PATH) -> list[Payload]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        Payload(id=p["id"], category=p["category"], text=p["text"], subtle=bool(p.get("subtle")))
        for p in spec["payloads"]
    ]


async def semantic_scores(embedder: Embedder, chunk_ids: list[str]) -> dict[str, float]:
    """Max cosine similarity to any exemplar, using embeddings the corpus already has.

    Computed in SQL against the stored vectors rather than by re-embedding: one query per
    exemplar instead of 74,000 forward passes.
    """
    vectors = embedder.encode_passages(list(EXEMPLARS))
    column = getattr(Chunk, embedder.space.column)
    best: dict[str, float] = {}

    async with admin_session() as session:
        for vector in vectors:
            distance = column.cosine_distance(vector.tolist())
            rows = (
                await session.execute(
                    select(Chunk.id, distance.label("d"))
                    .where(column.is_not(None))
                    .order_by(distance)
                    .limit(500)
                )
            ).all()
            for row in rows:
                similarity = 1.0 - float(row.d)
                key = str(row.id)
                if similarity > best.get(key, 0.0):
                    best[key] = similarity
    del chunk_ids
    return best


async def run(embedder: Embedder | None = None, worst_n: int = 8) -> InjectionReport:
    report = InjectionReport(semantic_used=embedder is not None)
    scorer = RuleScorer()

    # --- detection on planted payloads ------------------------------------
    payloads = load_payloads()
    report.payloads = len(payloads)
    exemplar_vectors = embedder.encode_passages(list(EXEMPLARS)) if embedder else None

    for payload in payloads:
        # Recorded, not scored. See the separation diagnostic below and the note in
        # `injection.py` on why the semantic layer was removed.
        if embedder is not None and exemplar_vectors is not None:
            vector = embedder.encode_passages([payload.text])[0]
            similarity = float(np.max(exemplar_vectors @ vector))
            bucket = report.subtle_similarity if payload.subtle else report.plain_similarity
            bucket.append(similarity)
        verdict = scorer.score(payload.text)
        hit, total = report.by_category.get(payload.category, (0, 0))
        if verdict.flagged:
            report.detected += 1
            hit += 1
        else:
            report.missed.append(f"{payload.id} ({payload.category})")
        report.by_category[payload.category] = (hit, total + 1)

    # --- false positives on the real corpus -------------------------------
    async with admin_session() as session:
        rows = (
            await session.execute(
                select(Chunk.id, Chunk.content, Document.path).join(
                    Document, Document.id == Chunk.document_id
                )
            )
        ).all()

    report.corpus_chunks = len(rows)
    scored: list[FalsePositive] = []
    for row in rows:
        row_verdict = scorer.score(row.content)
        if row_verdict.flagged:
            report.flagged += 1
            scored.append(
                FalsePositive(
                    path=row.path,
                    score=row_verdict.score,
                    signals=row_verdict.signals,
                    excerpt=" ".join(row.content.split())[:150],
                )
            )
        if row_verdict.quarantined:
            report.quarantined += 1

    report.worst = sorted(scored, key=lambda f: -f.score)[:worst_n]
    return report


def summarise(report: InjectionReport) -> str:
    lines = [
        f"payloads             {report.detected}/{report.payloads} detected "
        f"({report.detection_rate:.0%})",
        f"corpus chunks        {report.corpus_chunks:,}",
        f"flagged              {report.flagged:,} ({report.false_positive_rate:.3%})",
        f"quarantined          {report.quarantined:,}",
        f"flag threshold       {FLAG_THRESHOLD} · quarantine {QUARANTINE_THRESHOLD}",
        f"semantic layer       {'on' if report.semantic_used else 'off'}",
    ]
    if report.missed:
        lines.append("missed: " + ", ".join(report.missed))
    return "\n".join(lines)


def mean_or_zero(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0
