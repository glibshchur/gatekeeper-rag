# Retrieval ablation

58 hand-written questions over 73,801 chunks
of the GitLab Handbook. Embeddings: `bge-small-en-v1.5`. Reranker: `ms-marco-MiniLM-L-6-v2`.

Relevance is labelled at document level and every question is asked by a principal
entitled to the answer, so these numbers describe the system as deployed rather than
an unauthorized ideal. Percentages in brackets are relative to the dense baseline.

**Run-to-run variance.** `hnsw.iterative_scan = relaxed_order` makes the dense arm
non-deterministic at the k boundary, so these numbers move between runs. Observed
spread across four full runs on identical data: `dense` 0.002, `lexical` 0.003,
`dense+rerank` 0.003, `hybrid+rerank` 0.007 — and **`hybrid` 0.021**, an order
wider than the arm it is built from. Reciprocal Rank Fusion amplifies boundary
non-determinism rather than averaging it out.

Concretely: across those four runs `hybrid+rerank` finished ahead of
`dense+rerank` twice and behind it twice. Any single-run ranking of those two is
noise, which is exactly why the band is printed next to the table.

| configuration | hit@10 | recall@10 | MRR | nDCG@10 | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| **dense** — dense | 0.879 (=) | 0.871 (=) | 0.708 (=) | 0.748 (=) | 8 ms | 10 ms |
| **lexical** — lexical | 0.500 (-43%) | 0.491 (-44%) | 0.278 (-61%) | 0.329 (-56%) | 107 ms | 128 ms |
| **hybrid** — dense + lexical (RRF k=60) | 0.845 (-4%) | 0.836 (-4%) | 0.632 (-11%) | 0.678 (-9%) | 121 ms | 142 ms |
| **dense+rerank** — dense → cross-encoder | 0.879 (=) | 0.871 (=) | 0.773 (+9%) | 0.793 (+6%) | 205 ms | 240 ms |
| **hybrid+rerank** — dense + lexical (RRF k=60) → cross-encoder | 0.879 (=) | 0.871 (=) | 0.770 (+9%) | 0.791 (+6%) | 346 ms | 387 ms |

## Findings

**1. The cross-encoder is the only unambiguous win.** +6% nDCG and +9% MRR over the dense baseline. Note what
it does *not* change: `hit@10` and `recall@10` are identical, because reranking
reorders a candidate pool and cannot surface a document neither retriever
proposed. It buys ordering, not coverage — which is exactly what matters when the
answer is fed to a model with a limited context.

**2. Hybrid retrieval does not pay, and is not the default.** On its own it is
*worse* than dense alone — nDCG 0.678 against 0.748. Reciprocal Rank Fusion treats
its inputs as equally credible, and the lexical arm here is not: 0.329 alone against dense's 0.748,
so fusing it into the strong arm drags the strong arm down.

With a cross-encoder in front of it the damage is repaired but no benefit
appears: `hybrid+rerank` 0.791 against `dense+rerank` 0.793 is a difference of 0.002
against a noise band of 0.01, for 141 ms more per query. **`dense+rerank` is therefore
the shipped default.** The lexical arm stays implemented and stays in this table:
the category breakdown shows it genuinely helps where dense is weakest, and RRF
weighted by arm quality is untried. But a stage that costs 69% more latency for
nothing measurable does not belong switched on.

**3. A hypothesis this ablation killed.** The `lexical` question category —
questions built around identifiers and acronyms (STAR, CustomersDot, IMOC, SBOM) —
was written expecting lexical search to win it decisively, because embeddings are
structurally bad at tokens. It does not: dense 0.88, lexical 0.43, hybrid 0.83 on that category.

The reason is a Phase 1 decision made for an unrelated purpose. The chunker prefixes
each chunk's heading path into its text, so "STAR" and "CustomersDot" are *in the
embedded content* via the headings that name them. Structure-aware chunking removed
the weakness the lexical arm was meant to cover. Worth recording because it is the
kind of interaction that makes stage-by-stage ablation necessary: the value of a
component depends on decisions made elsewhere in the pipeline.

**4. Where the lexical arm helps and hurts**, from the category table below:
it helps `compensation` 0.82 → 1.00, `security` 0.43 → 0.50; hurts `support` 0.76 → 0.49, `engineering` 0.73 → 0.61, `culture` 0.94 → 0.83.
The pattern is consistent with RRF's flat weighting — it helps where dense is
weakest and hurts where dense is already strong.

**5. Bigger candidate pools buy nothing.** pool 50 is indistinguishable from the configured pool 20 (0.791 vs 0.798, a gap of 0.006 against a band of 0.01), while costing 1.9x the latency (667 ms against 351 ms). Below the knee the loss is real: pool 10 scores 0.761. Past the first ~20 candidates the pool is mostly irrelevant documents, so 20 stays the default in `RetrievalConfig` — chosen because more buys nothing, not because more is worse.


## nDCG@10 by question category

`lexical` is the set built around identifiers, acronyms and figures — the class
dense retrieval is structurally weak at. It is broken out so the hybrid arm's
advantage can be attributed rather than merely observed.

| configuration | compensation | culture | engineering | executive | finance | lexical | people | region | security | support |
|---|---|---|---|---|---|---|---|---|---|---|
| dense | 0.82 | 0.94 | 0.73 | 1.00 | 0.71 | 0.88 | 0.75 | 0.70 | 0.43 | 0.76 |
| lexical | 0.82 | 0.58 | 0.15 | 1.00 | 0.25 | 0.43 | 0.30 | 0.33 | 0.20 | 0.06 |
| hybrid | 1.00 | 0.83 | 0.61 | 1.00 | 0.65 | 0.83 | 0.65 | 0.65 | 0.50 | 0.49 |
| dense+rerank | 0.82 | 0.94 | 0.73 | 1.00 | 0.71 | 0.83 | 1.00 | 0.80 | 0.48 | 0.82 |
| hybrid+rerank | 0.82 | 0.94 | 0.73 | 1.00 | 0.71 | 0.83 | 1.00 | 0.80 | 0.50 | 0.77 |

## Candidate pool size on the winning arm

The cross-encoder is the entire latency budget and scales linearly in pool
size, so this is the knob worth measuring. `pool` is the number of candidates
each retriever proposes and the cap on what the reranker scores.

| pool | nDCG@10 | MRR | p50 | p95 |
|---:|---:|---:|---:|---:|
| 10 | 0.761 | 0.741 | 233 ms | 262 ms |
| 20 | 0.798 | 0.779 | 351 ms | 384 ms |
| 30 | 0.782 | 0.758 | 468 ms | 505 ms |
| 50 | 0.791 | 0.767 | 667 ms | 727 ms |

## Questions the best configuration still misses

Configuration: `dense+rerank`. A miss means no labelled document
appeared anywhere in the top 10.

| id | category | question |
|---|---|---|
| `fin-authorization` | finance | Who has authority to approve a large purchase or sign a contract? |
| `reg-global-expansion` | region | How does the company decide where to open a new legal entity for hiring? |
| `sec-overview` | security | What is the security team's mission and how is security organised? |
| `sec-operations` | security | What does the security operations team do day to day? |
| `sec-vuln-management` | security | How are vulnerabilities triaged and managed? |
| `eng-incident-followup` | engineering | What has to happen after an incident is resolved? |
| `lex-secret-push` | lexical | How is secret push protection performance tested? |
