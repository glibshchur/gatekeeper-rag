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

---

## Re-measured

**Date:** 2026-08-31 · **Phase:** 6

Re-ran the full ablation during packaging, on identical data, to check the numbers before
publishing them. The decision stands; two of the supporting claims do not, and that is
worth more than the confirmation.

nDCG@10 across four full runs on identical data:

| arm | run 1 | run 2 | run 3 | run 4 | spread |
|---|---:|---:|---:|---:|---:|
| dense | 0.750 | 0.748 | 0.748 | 0.748 | 0.002 |
| lexical | 0.339 | 0.341 | 0.338 | 0.341 | 0.003 |
| hybrid | 0.699 | 0.678 | 0.685 | 0.684 | **0.021** |
| dense+rerank | 0.796 | 0.793 | 0.793 | 0.793 | 0.003 |
| hybrid+rerank | 0.797 | 0.798 | 0.798 | 0.791 | 0.007 |

**The stated ±0.01 noise band is wrong for the fused arm.** Every other arm moves by
0.003 or less; `hybrid` spans 0.021 — an order wider than the arm it is built from.
Reciprocal Rank Fusion amplifies the dense arm's k-boundary non-determinism rather than
averaging it out. The generated variance note now reports the spread per arm instead of
quoting one band for all five.

**"Pool 20 beats pool 50 on quality" was an artifact of one run.** First run 0.797 vs
0.784 — a gap of 0.013 that reads as real. Second run 0.798 vs 0.791 — 0.007, which is
noise. What both runs support is that pool 50 costs 2× the latency for no measurable gain,
and that pool 10 is genuinely worse (0.768, 0.761). The default of 20 is unchanged; the
*reason* is now "more buys nothing" rather than "more is worse", which is the claim the
data actually carries.

**Across four runs, `hybrid+rerank` finished ahead of `dense+rerank` twice and behind it
twice** — 0.797/0.796, 0.798/0.793, 0.798/0.793, 0.791/0.793. Every gap is inside the
band. Any single run could be quoted as proof either way, which is the clearest possible
demonstration of why the band belongs next to the table: without it, run 2 reads as a
reason to reverse this ADR and run 4 reads as a reason to keep it, on the same data.

The findings prose in `evals/ablation.py` is now computed from the results rather than
typed in — it had already drifted (0.708 in the text against 0.699 in the table directly
above it), which is exactly how a generated report loses its authority.
