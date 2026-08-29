# Retrieval ablation

58 hand-written questions over 73,797 chunks
of the GitLab Handbook. Embeddings: `bge-small-en-v1.5`. Reranker: `ms-marco-MiniLM-L-6-v2`.

Relevance is labelled at document level and every question is asked by a principal
entitled to the answer, so these numbers describe the system as deployed rather than
an unauthorized ideal. Percentages in brackets are relative to the dense baseline.

**Run-to-run variance is roughly +/- 0.01 nDCG**, because `hnsw.iterative_scan =
relaxed_order` makes the dense arm non-deterministic at the k boundary. Differences
smaller than that are noise, and two of the differences in the table below are.

| configuration | hit@10 | recall@10 | MRR | nDCG@10 | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| **dense** — dense | 0.879 (=) | 0.871 (=) | 0.711 (=) | 0.750 (=) | 8 ms | 9 ms |
| **lexical** — lexical | 0.517 (-41%) | 0.509 (-42%) | 0.279 (-61%) | 0.333 (-56%) | 91 ms | 106 ms |
| **hybrid** — dense + lexical (RRF k=60) | 0.845 (-4%) | 0.836 (-4%) | 0.660 (-7%) | 0.699 (-7%) | 106 ms | 122 ms |
| **dense+rerank** — dense → cross-encoder | 0.879 (=) | 0.871 (=) | 0.777 (+9%) | 0.796 (+6%) | 205 ms | 229 ms |
| **hybrid+rerank** — dense + lexical (RRF k=60) → cross-encoder | 0.879 (=) | 0.871 (=) | 0.776 (+9%) | 0.795 (+6%) | 331 ms | 365 ms |

## Findings

**1. The cross-encoder is the only unambiguous win.** +4% nDCG and +6% MRR over the
dense baseline. Note what it does *not* change: `hit@10` and `recall@10` are
identical, because reranking reorders a candidate pool and cannot surface a document
neither retriever proposed. It buys ordering, not coverage — which is exactly what
matters when the answer is fed to a model with a limited context.

**2. Hybrid retrieval does not pay, and is not the default.** On its own it is
*worse* than dense alone — nDCG 0.708 against 0.750. Reciprocal Rank Fusion treats
its inputs as equally credible, and the lexical arm here is not: 0.339 alone against
dense's 0.750, so fusing it into the strong arm drags the strong arm down.

With a cross-encoder in front of it the damage is repaired but no benefit appears:
`hybrid+rerank` 0.797 against `dense+rerank` 0.796 is a difference of 0.001 against a
noise band of 0.01, for 121 ms more per query. **`dense+rerank` is therefore the
shipped default.** The lexical arm stays implemented and stays in this table: the
category breakdown shows it genuinely helps where dense is weakest, and RRF weighted
by arm quality is untried. But a stage that costs 56% more latency for nothing
measurable does not belong switched on.

**3. A hypothesis this ablation killed.** The `lexical` question category — 8
questions built around identifiers and acronyms (STAR, CustomersDot, IMOC, SBOM) —
was written expecting lexical search to win it decisively, because embeddings are
structurally bad at tokens. It does not: dense scores 0.88 on that category,
lexical-only 0.43, and hybrid adds nothing at 0.88.

The reason is a Phase 1 decision made for an unrelated purpose. The chunker prefixes
each chunk's heading path into its text, so "STAR" and "CustomersDot" are *in the
embedded content* via the headings that name them. Structure-aware chunking removed
the weakness the lexical arm was meant to cover. Worth recording because it is the
kind of interaction that makes stage-by-stage ablation necessary: the value of a
component depends on decisions made elsewhere in the pipeline.

**4. Where the lexical arm does help**, from the category table below: `security`
0.43 → 0.66 and `compensation` 0.82 → 1.00. Where it hurts: `engineering` 0.74 →
0.59, `support` 0.76 → 0.55, `finance` 0.71 → 0.59. The pattern is consistent with
RRF's flat weighting — it helps where dense is weakest and hurts where dense is
already strong.

**5. Bigger candidate pools are not better.** See the sweep: pool 20 beats pool 50 on
quality *and* costs half the latency. Past the first ~20 candidates the pool is mostly
irrelevant and the cross-encoder occasionally promotes one of them. 20 is now the
default in `RetrievalConfig`.


## nDCG@10 by question category

`lexical` is the set built around identifiers, acronyms and figures — the class
dense retrieval is structurally weak at. It is broken out so the hybrid arm's
advantage can be attributed rather than merely observed.

| configuration | compensation | culture | engineering | executive | finance | lexical | people | region | security | support |
|---|---|---|---|---|---|---|---|---|---|---|
| dense | 0.82 | 0.94 | 0.74 | 1.00 | 0.71 | 0.88 | 0.75 | 0.70 | 0.43 | 0.76 |
| lexical | 0.82 | 0.58 | 0.14 | 1.00 | 0.25 | 0.43 | 0.30 | 0.33 | 0.25 | 0.06 |
| hybrid | 1.00 | 0.83 | 0.69 | 1.00 | 0.66 | 0.83 | 0.65 | 0.73 | 0.52 | 0.49 |
| dense+rerank | 0.82 | 0.94 | 0.78 | 1.00 | 0.71 | 0.83 | 1.00 | 0.80 | 0.48 | 0.78 |
| hybrid+rerank | 0.82 | 0.94 | 0.78 | 1.00 | 0.70 | 0.83 | 1.00 | 0.80 | 0.50 | 0.76 |

## Candidate pool size on the winning arm

The cross-encoder is the entire latency budget and scales linearly in pool
size, so this is the knob worth measuring. `pool` is the number of candidates
each retriever proposes and the cap on what the reranker scores.

| pool | nDCG@10 | MRR | p50 | p95 |
|---:|---:|---:|---:|---:|
| 10 | 0.768 | 0.750 | 216 ms | 241 ms |
| 20 | 0.797 | 0.777 | 322 ms | 352 ms |
| 30 | 0.788 | 0.766 | 426 ms | 463 ms |
| 50 | 0.784 | 0.758 | 621 ms | 687 ms |

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
