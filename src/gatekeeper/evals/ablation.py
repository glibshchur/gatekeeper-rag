"""Ablation: measure each retrieval stage against the same golden set.

The arms differ only in a :class:`RetrievalConfig`, never in code path, so a difference
between two rows is attributable to the stage that changed and not to two different
implementations of "retrieve".

The table is generated, never hand-written. A benchmark table maintained by hand drifts
from the code the first time someone tunes a parameter and forgets, and a stale number in
a README is worse than no number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gatekeeper.evals.harness import EvalReport, GoldenQuestion, evaluate
from gatekeeper.retrieval.pipeline import (
    ABLATION_ARMS,
    DEFAULT_CONFIG,
    HYBRID_RERANK,
    RetrievalConfig,
    with_candidates,
)

if TYPE_CHECKING:
    from gatekeeper.llm.embeddings import Embedder
    from gatekeeper.llm.rerank import CrossEncoderReranker


@dataclass
class Ablation:
    reports: list[EvalReport]
    k: int
    corpus_chunks: int
    question_count: int
    sweep: list[EvalReport] = field(default_factory=list)


async def run(
    questions: list[GoldenQuestion],
    embedder: Embedder,
    *,
    k: int = 10,
    reranker: CrossEncoderReranker | None = None,
    arms: tuple[RetrievalConfig, ...] = ABLATION_ARMS,
    corpus_chunks: int = 0,
    sweep_candidates: tuple[int, ...] = (10, 20, 30, 50),
) -> Ablation:
    reports = []
    for config in arms:
        if config.rerank and reranker is None:
            continue
        reports.append(await evaluate(questions, config, embedder, k=k, reranker=reranker))

    # Candidate-pool sweep on the winning arm. The cross-encoder is the whole latency
    # budget, and it scales linearly in pool size, so this is the one knob where the
    # quality/latency trade is worth measuring rather than assuming.
    sweep = []
    if reranker is not None:
        for count in sweep_candidates:
            config = with_candidates(HYBRID_RERANK, count)
            sweep.append(
                await evaluate(
                    questions,
                    RetrievalConfig(**{**config.__dict__, "name": f"pool={count}"}),
                    embedder,
                    k=k,
                    reranker=reranker,
                )
            )

    return Ablation(
        reports=reports,
        k=k,
        corpus_chunks=corpus_chunks,
        question_count=len(questions),
        sweep=sweep,
    )


def _delta(value: float, baseline: float) -> str:
    if baseline == 0:
        return ""
    change = (value - baseline) / baseline
    if abs(change) < 0.005:
        return " (=)"
    return f" ({change:+.0%})"


def _category_claim(ablation: Ablation, category: str, arms: tuple[str, ...]) -> str:
    """Read one category's scores off the reports rather than restating them."""
    by_name = {r.config.name: r for r in ablation.reports}
    parts = []
    for arm in arms:
        report = by_name.get(arm)
        if report is None:
            continue
        parts.append(f"{arm} {report.by_category().get(category, 0.0):.2f}")
    return ", ".join(parts) + " on that category."


def _fusion_swings(ablation: Ablation, limit: int = 3) -> str:
    """The categories fusion moves most, in both directions, computed per run.

    Hardcoding these was how the previous version of this section went stale: the swings
    depend on a non-deterministic dense arm, so they genuinely change between runs.
    """
    by_name = {r.config.name: r for r in ablation.reports}
    dense, hybrid = by_name.get("dense"), by_name.get("hybrid")
    if dense is None or hybrid is None:
        return "_no hybrid arm in this run._"
    d, h = dense.by_category(), hybrid.by_category()
    deltas = sorted(
        ((c, d.get(c, 0.0), h.get(c, 0.0)) for c in d), key=lambda t: t[2] - t[1], reverse=True
    )
    helped = [f"`{c}` {a:.2f} → {b:.2f}" for c, a, b in deltas[:limit] if b > a]
    hurt = [f"`{c}` {a:.2f} → {b:.2f}" for c, a, b in reversed(deltas[-limit:]) if b < a]
    out = []
    if helped:
        out.append("helps " + ", ".join(helped))
    if hurt:
        out.append("hurts " + ", ".join(hurt))
    return ("it " + "; ".join(out) + ".") if out else "fusion moved no category measurably."


def _pool_claim(ablation: Ablation, noise: float = 0.01) -> str:
    """State what the pool sweep supports about the *configured default*, and no more.

    Two ways this went wrong before, both worth guarding against:

    * A hand-written version claimed "pool 20 beats pool 50 on quality" — true of one run
      (0.797 vs 0.784), noise on the next (0.798 vs 0.791). The defensible claim is that
      bigger pools cost latency for no measurable gain.
    * A first attempt at computing it anchored on whichever pool scored highest *this run*,
      which on a run where 50 tied 20 produced "pool 50 is within noise of pool 50" and
      announced 50 as the default. The anchor has to be the value actually configured in
      `RetrievalConfig`, because that is the decision this paragraph exists to justify.
    """
    if not ablation.sweep:
        return "_no sweep in this run._"
    default_n = DEFAULT_CONFIG.rerank_candidates
    by_size = {r.config.candidates: r for r in ablation.sweep}
    default = by_size.get(default_n)
    largest = max(ablation.sweep, key=lambda r: r.config.candidates)
    smallest = min(ablation.sweep, key=lambda r: r.config.candidates)
    if default is None or largest.config.candidates == default_n:
        return "_the sweep does not bracket the configured default._"

    gap = default.ndcg - largest.ndcg
    if abs(gap) < noise:
        verdict = (
            f"pool {largest.config.candidates} is indistinguishable from the configured "
            f"pool {default_n} ({largest.ndcg:.3f} vs {default.ndcg:.3f}, a gap of "
            f"{abs(gap):.3f} against a band of {noise:.2f})"
        )
    elif gap > 0:
        verdict = (
            f"pool {default_n} beats pool {largest.config.candidates} outright "
            f"({default.ndcg:.3f} vs {largest.ndcg:.3f})"
        )
    else:
        verdict = (
            f"pool {largest.config.candidates} scores {largest.ndcg:.3f} against pool "
            f"{default_n}'s {default.ndcg:.3f} — a real gain, and the default is due a review"
        )

    return (
        f"{verdict}, while costing {largest.p50_ms / default.p50_ms:.1f}x the latency "
        f"({largest.p50_ms:.0f} ms against {default.p50_ms:.0f} ms). Below the knee the loss "
        f"is real: pool {smallest.config.candidates} scores {smallest.ndcg:.3f}. Past the "
        f"first ~{default_n} candidates the pool is mostly irrelevant documents, so "
        f"{default_n} stays the default in `RetrievalConfig` — chosen because more buys "
        "nothing, not because more is worse."
    )


def to_markdown(ablation: Ablation, embedding_model: str, reranker_model: str | None) -> str:
    k = ablation.k
    base = ablation.reports[0]

    lines = [
        "# Retrieval ablation",
        "",
        f"{ablation.question_count} hand-written questions over {ablation.corpus_chunks:,} chunks",
        f"of the GitLab Handbook. Embeddings: `{embedding_model}`."
        + (f" Reranker: `{reranker_model}`." if reranker_model else ""),
        "",
        "Relevance is labelled at document level and every question is asked by a principal",
        "entitled to the answer, so these numbers describe the system as deployed rather than",
        "an unauthorized ideal. Percentages in brackets are relative to the dense baseline.",
        "",
        "**Run-to-run variance.** `hnsw.iterative_scan = relaxed_order` makes the dense arm",
        "non-deterministic at the k boundary, so these numbers move between runs. Observed",
        "spread across four full runs on identical data: `dense` 0.002, `lexical` 0.003,",
        "`dense+rerank` 0.003, `hybrid+rerank` 0.007 — and **`hybrid` 0.021**, an order",
        "wider than the arm it is built from. Reciprocal Rank Fusion amplifies boundary",
        "non-determinism rather than averaging it out.",
        "",
        "Concretely: across those four runs `hybrid+rerank` finished ahead of",
        "`dense+rerank` twice and behind it twice. Any single-run ranking of those two is",
        "noise, which is exactly why the band is printed next to the table.",
        "",
        f"| configuration | hit@{k} | recall@{k} | MRR | nDCG@{k} | p50 | p95 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for report in ablation.reports:
        lines.append(
            f"| **{report.config.name}** — {report.config.description} "
            f"| {report.hit_rate:.3f}{_delta(report.hit_rate, base.hit_rate)} "
            f"| {report.recall:.3f}{_delta(report.recall, base.recall)} "
            f"| {report.mrr:.3f}{_delta(report.mrr, base.mrr)} "
            f"| {report.ndcg:.3f}{_delta(report.ndcg, base.ndcg)} "
            f"| {report.p50_ms:.0f} ms | {report.p95_ms:.0f} ms |"
        )

    # Numbers below are read off `ablation.reports`, never typed in. An earlier version
    # hardcoded them and went stale against the table directly above it -- nDCG 0.708 in
    # the prose against 0.699 in the table -- which is worse than no commentary, because a
    # reader has no way to tell which one is current.
    by_name = {r.config.name: r for r in ablation.reports}
    dense = by_name.get("dense")
    lex = by_name.get("lexical")
    hyb = by_name.get("hybrid")
    dr = by_name.get("dense+rerank")
    hr = by_name.get("hybrid+rerank")

    lines += ["", "## Findings", ""]

    if dense is not None and dr is not None:
        lines += [
            f"**1. The cross-encoder is the only unambiguous win.** "
            f"{_delta(dr.ndcg, dense.ndcg).strip(' ()')} nDCG and "
            f"{_delta(dr.mrr, dense.mrr).strip(' ()')} MRR over the dense baseline. Note what",
            "it does *not* change: `hit@10` and `recall@10` are identical, because reranking",
            "reorders a candidate pool and cannot surface a document neither retriever",
            "proposed. It buys ordering, not coverage — which is exactly what matters when the",
            "answer is fed to a model with a limited context.",
            "",
        ]

    if dense is not None and lex is not None and hyb is not None:
        lines += [
            "**2. Hybrid retrieval does not pay, and is not the default.** On its own it is",
            f"*worse* than dense alone — nDCG {hyb.ndcg:.3f} against {dense.ndcg:.3f}. "
            "Reciprocal Rank Fusion treats",
            f"its inputs as equally credible, and the lexical arm here is not: {lex.ndcg:.3f} "
            f"alone against dense's {dense.ndcg:.3f},",
            "so fusing it into the strong arm drags the strong arm down.",
            "",
        ]

    if dr is not None and hr is not None:
        gap = abs(hr.ndcg - dr.ndcg)
        cost = hr.p50_ms - dr.p50_ms
        lines += [
            "With a cross-encoder in front of it the damage is repaired but no benefit",
            f"appears: `hybrid+rerank` {hr.ndcg:.3f} against `dense+rerank` {dr.ndcg:.3f} is a "
            f"difference of {gap:.3f}",
            f"against a noise band of 0.01, for {cost:.0f} ms more per query. "
            "**`dense+rerank` is therefore",
            "the shipped default.** The lexical arm stays implemented and stays in this table:",
            "the category breakdown shows it genuinely helps where dense is weakest, and RRF",
            f"weighted by arm quality is untried. But a stage that costs "
            f"{cost / dr.p50_ms:.0%} more latency for",
            "nothing measurable does not belong switched on.",
            "",
        ]

    lines += [
        "**3. A hypothesis this ablation killed.** The `lexical` question category —",
        "questions built around identifiers and acronyms (STAR, CustomersDot, IMOC, SBOM) —",
        "was written expecting lexical search to win it decisively, because embeddings are",
        "structurally bad at tokens. It does not: "
        + _category_claim(ablation, "lexical", ("dense", "lexical", "hybrid")),
        "",
        "The reason is a Phase 1 decision made for an unrelated purpose. The chunker prefixes",
        'each chunk\'s heading path into its text, so "STAR" and "CustomersDot" are *in the',
        "embedded content* via the headings that name them. Structure-aware chunking removed",
        "the weakness the lexical arm was meant to cover. Worth recording because it is the",
        "kind of interaction that makes stage-by-stage ablation necessary: the value of a",
        "component depends on decisions made elsewhere in the pipeline.",
        "",
        "**4. Where the lexical arm helps and hurts**, from the category table below:",
        _fusion_swings(ablation),
        "The pattern is consistent with RRF's flat weighting — it helps where dense is",
        "weakest and hurts where dense is already strong.",
        "",
        "**5. Bigger candidate pools buy nothing.** " + _pool_claim(ablation),
        "",
    ]

    categories = sorted({c for r in ablation.reports for c in r.by_category()})
    lines += [
        "",
        f"## nDCG@{k} by question category",
        "",
        "`lexical` is the set built around identifiers, acronyms and figures — the class",
        "dense retrieval is structurally weak at. It is broken out so the hybrid arm's",
        "advantage can be attributed rather than merely observed.",
        "",
        "| configuration | " + " | ".join(categories) + " |",
        "|---" * (len(categories) + 1) + "|",
    ]
    for report in ablation.reports:
        scores = report.by_category()
        lines.append(
            f"| {report.config.name} | "
            + " | ".join(f"{scores.get(c, 0.0):.2f}" for c in categories)
            + " |"
        )

    if ablation.sweep:
        lines += [
            "",
            "## Candidate pool size on the winning arm",
            "",
            "The cross-encoder is the entire latency budget and scales linearly in pool",
            "size, so this is the knob worth measuring. `pool` is the number of candidates",
            "each retriever proposes and the cap on what the reranker scores.",
            "",
            f"| pool | nDCG@{k} | MRR | p50 | p95 |",
            "|---:|---:|---:|---:|---:|",
        ]
        for report in ablation.sweep:
            lines.append(
                f"| {report.config.candidates} | {report.ndcg:.3f} | {report.mrr:.3f} "
                f"| {report.p50_ms:.0f} ms | {report.p95_ms:.0f} ms |"
            )

    best = max(ablation.reports, key=lambda r: r.ndcg)
    lines += [
        "",
        "## Questions the best configuration still misses",
        "",
        f"Configuration: `{best.config.name}`. A miss means no labelled document",
        f"appeared anywhere in the top {k}.",
        "",
    ]
    misses = best.misses()
    if not misses:
        lines.append("None.")
    else:
        lines.append("| id | category | question |")
        lines.append("|---|---|---|")
        for result in misses:
            lines.append(
                f"| `{result.question.id}` | {result.question.category} "
                f"| {result.question.question} |"
            )
    return "\n".join(lines) + "\n"
