"""Retrieval evaluation.

Metrics are computed at **document** granularity: the top-k chunks are collapsed to the
ordered list of distinct documents they came from, and that list is scored against the
labelled set. Two consequences to keep in mind when reading the numbers:

* Recall@10 over documents is more forgiving than over chunks, because ten chunks can
  come from three documents. It is still the question a user has.
* A configuration that returns ten chunks from one correct document scores the same as
  one that returns one chunk from it. Reranking, which concentrates results, is therefore
  not rewarded for concentration by these metrics — only for correctness.

Every question is run **as the principal labelled to ask it**, so the numbers describe
the system as deployed rather than an unauthorized ideal.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import select

from gatekeeper.core.db import admin_session, principal_session
from gatekeeper.core.models import Document
from gatekeeper.ingest import seed
from gatekeeper.retrieval.pipeline import RetrievalConfig, retrieve

if TYPE_CHECKING:
    from gatekeeper.core.principal import Principal
    from gatekeeper.llm.embeddings import Embedder
    from gatekeeper.llm.rerank import CrossEncoderReranker

GOLDEN_PATH = Path(__file__).parent / "golden.yaml"


@dataclass(frozen=True)
class GoldenQuestion:
    id: str
    question: str
    relevant: tuple[str, ...]
    asker: str
    category: str


@dataclass
class QuestionResult:
    question: GoldenQuestion
    retrieved: list[str]
    latency_ms: int

    @property
    def hit(self) -> bool:
        return bool(set(self.retrieved) & set(self.question.relevant))

    def recall(self) -> float:
        found = len(set(self.retrieved) & set(self.question.relevant))
        return found / len(self.question.relevant)

    def reciprocal_rank(self) -> float:
        for rank, path in enumerate(self.retrieved, start=1):
            if path in self.question.relevant:
                return 1.0 / rank
        return 0.0

    def ndcg(self, k: int = 10) -> float:
        """Binary-gain nDCG. Ideal DCG assumes every labelled document ranks first."""
        gains = [1.0 if p in self.question.relevant else 0.0 for p in self.retrieved[:k]]
        dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
        ideal_hits = min(len(self.question.relevant), k)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
        return dcg / idcg if idcg else 0.0


@dataclass
class EvalReport:
    config: RetrievalConfig
    results: list[QuestionResult] = field(default_factory=list)

    def _mean(self, fn: Any, subset: list[QuestionResult] | None = None) -> float:
        rows = subset if subset is not None else self.results
        return statistics.fmean(fn(r) for r in rows) if rows else 0.0

    @property
    def hit_rate(self) -> float:
        return self._mean(lambda r: 1.0 if r.hit else 0.0)

    @property
    def recall(self) -> float:
        return self._mean(lambda r: r.recall())

    @property
    def mrr(self) -> float:
        return self._mean(lambda r: r.reciprocal_rank())

    @property
    def ndcg(self) -> float:
        return self._mean(lambda r: r.ndcg())

    @property
    def p50_ms(self) -> float:
        values = sorted(r.latency_ms for r in self.results)
        return float(values[len(values) // 2]) if values else 0.0

    @property
    def p95_ms(self) -> float:
        values = sorted(r.latency_ms for r in self.results)
        return float(values[min(int(len(values) * 0.95), len(values) - 1)]) if values else 0.0

    def by_category(self) -> dict[str, float]:
        buckets: dict[str, list[QuestionResult]] = {}
        for result in self.results:
            buckets.setdefault(result.question.category, []).append(result)
        return {
            name: self._mean(lambda r: r.ndcg(), rows) for name, rows in sorted(buckets.items())
        }

    def misses(self) -> list[QuestionResult]:
        return [r for r in self.results if not r.hit]


def load_golden(path: Path = GOLDEN_PATH) -> list[GoldenQuestion]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        GoldenQuestion(
            id=q["id"],
            question=q["question"],
            relevant=tuple(q["relevant"]),
            asker=q["asker"],
            category=q["category"],
        )
        for q in spec["questions"]
    ]


async def validate_golden(questions: list[GoldenQuestion]) -> list[str]:
    """Check the labels before trusting anything computed from them.

    Two failures are possible and both are silent otherwise. A path that does not exist
    makes a question permanently unanswerable, dragging every metric down for a reason
    that has nothing to do with retrieval. A path the asker cannot *see* is worse: it
    looks like a retrieval failure and is actually a mislabelled question, and chasing it
    would mean tuning the retriever against the access model.
    """
    problems: list[str] = []
    async with admin_session() as session:
        known = {
            row[0]
            for row in await session.execute(select(Document.path).where(Document.source != ""))
        }

    principals: dict[str, Principal] = {}
    for question in questions:
        missing = [p for p in question.relevant if p not in known]
        if missing:
            problems.append(f"{question.id}: no such document(s) {missing}")
            continue
        if question.asker not in principals:
            principals[question.asker] = await seed.load_principal(question.asker)
        principal = principals[question.asker]
        async with principal_session(principal) as session:
            visible = {
                row[0]
                for row in await session.execute(
                    select(Document.path).where(Document.path.in_(question.relevant))
                )
            }
        unreachable = sorted(set(question.relevant) - visible)
        if unreachable:
            problems.append(
                f"{question.id}: asker {question.asker!r} cannot read {unreachable} — "
                "the label is wrong, or the access model is"
            )
    return problems


async def evaluate(
    questions: list[GoldenQuestion],
    config: RetrievalConfig,
    embedder: Embedder,
    *,
    k: int = 10,
    reranker: CrossEncoderReranker | None = None,
) -> EvalReport:
    report = EvalReport(config=config)
    principals: dict[str, Principal] = {}

    for question in questions:
        if question.asker not in principals:
            principals[question.asker] = await seed.load_principal(question.asker)
        principal = principals[question.asker]

        started = time.monotonic()
        result = await retrieve(
            principal,
            question.question,
            embedder,
            config=config,
            k=k,
            reranker=reranker,
            # Auditing every eval query would serialise the run on the chain's advisory
            # lock and bury real activity under thousands of synthetic entries.
            audit=False,
        )
        elapsed = int((time.monotonic() - started) * 1000)

        ordered_docs: list[str] = []
        for chunk in result.chunks:
            if chunk.path not in ordered_docs:
                ordered_docs.append(chunk.path)

        report.results.append(
            QuestionResult(question=question, retrieved=ordered_docs, latency_ms=elapsed)
        )
    return report
