# 0008 — Hybrid search: built, measured, and off by default

**Status:** Accepted · **Date:** 2026-08-29 · **Phase:** 3

## Context

Dense retrieval fails predictably: it is good at meaning and bad at tokens. "SEC-4417",
"IRS Form 1099", "25 USD" have no useful neighbourhood in embedding space, and a policy
corpus is full of exactly those. The standard remedy is hybrid retrieval — run a lexical
retriever alongside the dense one and fuse the rankings — and it is on nearly every
"production RAG" checklist.

So it was built: a `tsvector` index, `ts_rank_cd` ranking, Reciprocal Rank Fusion, and a
cross-encoder reranker, with every stage expressed as a field on `RetrievalConfig` so the
arms of the comparison differ only in data.

Then it was measured against 58 hand-written questions
([`docs/ABLATION.md`](../ABLATION.md)).

## What the measurement said

| arm | nDCG@10 | p50 |
|---|---:|---:|
| dense | 0.750 | 8 ms |
| lexical | 0.339 | 92 ms |
| hybrid | **0.708** | 107 ms |
| dense + rerank | 0.796 | 215 ms |
| hybrid + rerank | 0.797 | 336 ms |

**Hybrid on its own is worse than dense on its own.** RRF treats its inputs as equally
credible, and these are not: the lexical arm scores 0.339 against dense's 0.750. Fusing a
weak ranker into a strong one drags the strong one down.

**With a reranker the damage is repaired and no benefit appears.** 0.797 against 0.796 is
a difference of 0.001 against a run-to-run noise band of ±0.01, for 56% more latency.

## Decision

`dense+rerank` is the shipped default. The lexical arm stays implemented, stays in the
ablation, and stays off.

Keeping it rather than deleting it is a deliberate call: the per-category breakdown shows
it genuinely helps where dense is weakest (`security` 0.43 → 0.66, `compensation` 0.82 →
1.00), and RRF weighted by arm quality is untried. Deleting the code would throw away the
measurement infrastructure along with it.

## Consequences

The cross-encoder is the only unambiguous win in the whole phase: +6% MRR, +4% nDCG. Note
what it does *not* change — `hit@10` and `recall@10` are identical, because reranking
reorders a candidate pool and cannot surface a document neither retriever proposed. It buys
ordering, not coverage, which is what matters when results are fed to a model with limited
context.

Candidate pool size was also measured rather than assumed, and the intuition was wrong:
pool 20 beats pool 50 on quality *and* costs half the latency (0.797 vs 0.784, 336 ms vs
637 ms). Past the first ~20 candidates the pool is mostly irrelevant and the cross-encoder
occasionally promotes one. The default is now 20.

## A hypothesis this killed

Eight of the 58 questions form a `lexical` category built around identifiers and acronyms
— STAR, CustomersDot, IMOC, SBOM — written expecting the lexical arm to win them
decisively. It does not: dense 0.88, lexical-only 0.43, hybrid 0.88. No gain at all on the
category designed to showcase it.

The reason is a Phase 1 decision taken for an unrelated purpose. The chunker prefixes each
chunk's heading path into its text, so "STAR" and "CustomersDot" are *in the embedded
content* via the headings that name them. Structure-aware chunking had already removed the
weakness the lexical arm was meant to cover.

This is the argument for stage-by-stage ablation rather than adding known-good components:
the value of a component depends on decisions made elsewhere in the pipeline, and there is
no way to know which without measuring the specific combination.
