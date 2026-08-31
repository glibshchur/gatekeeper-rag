# I built hybrid search, measured it, and turned it off

*An ablation study over 58 hand-written questions, and the three hypotheses it killed.*

---

The standard advice for improving RAG retrieval is a stack: dense embeddings, plus BM25,
fused with Reciprocal Rank Fusion, then a cross-encoder reranker. I built all of it, over
73,801 chunks of the GitLab handbook, and measured each stage.

One of the three stages was worth keeping.

| configuration | nDCG@10 | MRR | recall@10 | p50 |
|---|---:|---:|---:|---:|
| dense | 0.748 | 0.708 | 0.871 | 8 ms |
| lexical only | 0.341 | 0.289 | 0.509 | 105 ms |
| dense + lexical (RRF) | 0.684 | 0.640 | 0.836 | 119 ms |
| **dense + rerank** | **0.793** | **0.773** | 0.871 | 205 ms |
| dense + lexical + rerank | 0.791 | 0.770 | 0.871 | 341 ms |

**Publish the variance band or the table is decorative.** `hnsw.iterative_scan =
relaxed_order` makes the dense arm non-deterministic at the k boundary, so every number
here moves between runs. Observed spread across **four** full runs on identical data:

| arm | run 1 | run 2 | run 3 | run 4 | spread |
|---|---:|---:|---:|---:|---:|
| dense | 0.750 | 0.748 | 0.748 | 0.748 | 0.002 |
| lexical | 0.339 | 0.341 | 0.338 | 0.341 | 0.003 |
| hybrid | 0.699 | 0.678 | 0.685 | 0.684 | **0.021** |
| dense+rerank | 0.796 | 0.793 | 0.793 | 0.793 | 0.003 |
| hybrid+rerank | 0.797 | 0.798 | 0.798 | 0.791 | 0.007 |

That hybrid row is itself a finding. Reciprocal Rank Fusion **amplifies** boundary
non-determinism rather than averaging it out — the fused arm is an order noisier than the
arm it's built from. I only know that because I ran the whole thing four times, which is
not something I'd have bothered doing if the first run had told me what I wanted to hear.

## Getting the eval right first

Before any of this, the golden set — and one decision determined whether the whole study
would mean anything.

**The questions are hand-written by reading the corpus.** The tempting shortcut is to
sample a chunk and turn its own sentences into a query. That makes the eval **circular**:
the query is drawn from the document it's supposed to retrieve, so lexical search wins by
construction and the dense-vs-hybrid comparison measures nothing but overlap. Every
question here is one someone would actually type.

**Relevance is labelled at document level**, not chunk level. Cheaper, and it matches the
question a user actually has — did it find the right document? A consequence worth stating:
recall@10 over documents is more forgiving than over chunks, because ten chunks can come
from fewer than ten documents.

**Every question is asked by a principal entitled to the answer.** Metrics are computed
under that principal's authorization, so they describe the system as deployed rather than
an unauthorized ideal. A validation pass fails the run if any label is unreachable — a
mislabelled path would otherwise drag every metric down for a reason that has nothing to
do with retrieval.

And the arms of the ablation differ **only in a config object**, never in a code path. A
comparison whose arms are separate code paths is a comparison of two implementations, and
any difference between them is a candidate explanation for the result.

## Hypothesis 1: hybrid search will help. It didn't.

Fused with dense, lexical made things **worse** — 0.684 against dense's 0.748.

Reciprocal Rank Fusion treats its inputs as equally credible. The lexical arm here is not
equally credible: 0.341 alone against dense's 0.748. Fusing a weak ranker into a strong one
at equal weight drags the strong one down. That's not a flaw in RRF, it's what RRF is; I'd
reached for it without checking that its assumption held.

With a cross-encoder downstream the damage is repaired, and the two arms become
indistinguishable. Across four runs `hybrid+rerank` finished **ahead of `dense+rerank`
twice and behind it twice** — 0.797/0.796, 0.798/0.793, 0.798/0.793, 0.791/0.793. Every
gap is inside the band. Any one of those runs could be quoted as proof either way, and it
costs **136 ms more per query** to obtain the tie. So hybrid is off by default. It stays implemented and stays in the
table, because the category breakdown shows it genuinely helps where dense is weakest and
RRF weighted by arm quality is untried. But a stage that costs 66% more latency for
nothing measurable doesn't ship switched on.

Worth being explicit about the discipline here: if I'd run this once, seen 0.798 against
0.793, and shipped hybrid as "the better arm", I'd have been reporting noise as a finding —
and the very next run would have contradicted me on identical data. The variance band is
what stops that, and it only exists because I bothered to measure it.

## Hypothesis 2: lexical will win on identifiers. It didn't.

I wrote a question category specifically for this — 8 questions built around identifiers
and acronyms (STAR, CustomersDot, IMOC, SBOM) — expecting lexical to win decisively.
Embeddings are structurally bad at rare tokens. This is the textbook case for hybrid.

Dense scored **0.88** on that category. Lexical-only: 0.43. Hybrid added nothing: 0.88.

The reason is a decision made months earlier for an unrelated purpose. The chunker prefixes
each chunk's **heading path** into its text, so "STAR" and "CustomersDot" are already in
the embedded content, via the headings that name them. Structure-aware chunking had quietly
removed the weakness the lexical arm was meant to cover.

This is the strongest argument I know for stage-by-stage ablation over reasoning from first
principles: **the value of a component depends on decisions made elsewhere in the
pipeline**, and no amount of thinking about embeddings would have told me that my chunker
had already solved it.

Where lexical *does* help, from the category table: `security` 0.43 → 0.66, `compensation`
0.82 → 1.00. Where it hurts: `engineering` 0.74 → 0.59, `support` 0.76 → 0.55. Consistent
with flat-weighted fusion — it helps where dense is weakest and hurts where dense is
already strong.

## Hypothesis 3: bigger candidate pools are better. They aren't.

More candidates can only give the reranker more to work with. That's the intuition, and
it's wrong:

| pool | nDCG@10 | p50 |
|---:|---:|---:|
| 10 | 0.761 | 224 ms |
| **20** | **0.798** | 338 ms |
| 30 | 0.782 | 440 ms |
| 50 | 0.791 | 649 ms |

Pool 50 costs **2× the latency of pool 20 for no measurable gain** — 0.791 against 0.798
is inside the noise band.

I originally wrote this section as "pool 50 scores *worse*", because on the first run it
did: 0.784 against 0.797, a gap of 0.013 that looked real. On the second run the gap was
0.007, which is noise. **The stronger claim was an artifact of running it once.** What
both runs support is that bigger pools cost latency and buy nothing, and that pool 10 is
genuinely worse (0.761 and 0.768 — consistently below). So 20 is the default because more
buys nothing, not because more is worse.

That distinction sounds pedantic and isn't. "Bigger is worse" implies a mechanism —
irrelevant candidates actively confusing the reranker — that I'd have gone on to explain,
confidently, on the basis of one run.

## What the reranker actually buys

The one unambiguous win: **+6% nDCG, +9% MRR** over the dense baseline — comfortably
outside the noise band, unlike everything else in the table.

Note what it does **not** change. `hit@10` and `recall@10` are identical to the dense
baseline — reranking reorders a candidate pool and cannot surface a document neither
retriever proposed. It buys **ordering, not coverage**.

For a RAG system that's exactly the right thing to buy. The answer is generated from the
top few chunks in a limited context window, so moving the right document from position 7
to position 2 changes the answer even though every recall metric stays flat.

## Where it still fails

Seven of 58 questions return no labelled document anywhere in the top 10. Five of the
seven are broad organisational questions — *"What is the security team's mission?"*, *"What
does the security operations team do day to day?"* — where the answer is diffused across a
whole subtree rather than concentrated in a passage. Chunk-level retrieval is the wrong
shape for that question, and no reranker fixes it. Published rather than trimmed, because
the failure pattern is more informative than the aggregate.

## The finding that was my own bug

An earlier version of this study reported that authorization filtering cost nothing and
recall was a perfect 1.000 at every selectivity level. Both were measurement artifacts —
prepared-statement plan reuse across a planner setting change, and buffer-pool eviction
from computing ground truth.

Recall of exactly 1.000 everywhere should have been suspicious on sight. It wasn't, because
it was the number I was hoping for. **A benchmark that reports what you expected is not
evidence**, and I now treat a clean result as a prompt to check the harness before checking
the champagne.

## What transfers

**Write the eval before the optimisation.** Otherwise you're tuning against a metric you
invented afterwards to justify what you built.

**Never generate eval questions from the text you're retrieving.** It's circular and it
silently favours lexical methods.

**Publish the variance band.** Half the differences in a typical ablation table are noise,
and you cannot tell which without it.

**Make the arms differ in data, not code.** Otherwise you're comparing implementations.

**Report the stages that didn't pay.** They're the ones that tell a reader you measured
rather than assembled. Two of the three stages here were expensive no-ops, and knowing
which two is worth more than the 0.05 nDCG the third one bought.

---

*From [gatekeeper-rag](https://github.com/OWNER/gatekeeper-rag). Reproduce with
`make eval`; the generated table is [docs/ABLATION.md](../ABLATION.md) and the decision is
[ADR 0008](../adr/0008-hybrid-search-measured-and-disabled.md).*
