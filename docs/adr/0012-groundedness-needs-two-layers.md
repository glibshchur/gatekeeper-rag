# 0012 — Groundedness needs a numeric check, because embeddings cannot see numbers

**Status:** Accepted · **Date:** 2026-08-30 · **Phase:** 4

## Context

Citation markers prove nothing. A model can write "[2]" after a sentence source 2 does not
support, and any mechanism that counts brackets will call the answer grounded. The
verifier therefore splits an answer into sentences and asks, per sentence, whether a cited
chunk actually supports it — by cosine similarity against the source passages.

The intent was to note the obvious limitation (similarity is not entailment, so
contradictions slip through) and ship. Measuring it against deliberate corruptions of one
source sentence produced a different and more useful picture:

| corruption | similarity | similarity alone |
|---|---:|---|
| faithful restatement | 0.884 | supported (correct) |
| negation — "receipts are **not** required" | 0.673 | flagged (correct) |
| **75 USD → 750 USD** | 0.867 | **supported — wrong** |
| **75 USD → 7 USD** | 0.872 | **supported — wrong** |
| **30 days → 300 days** | 0.775 | **supported — wrong** |
| reordered causality | 0.512 | flagged (correct) |
| outright fabrication | 0.544 | flagged (correct) |

Negation *is* caught — the opposite of what the module was written assuming. The real
blind spot is **numbers**: a limit changed by a factor of ten scores 0.867 against a
faithful 0.884, which is statistically indistinguishable.

That is the worst possible place for this system to be blind. A policy corpus *is*
thresholds, limits and deadlines, and a confidently wrong expense limit with a citation
attached is precisely the failure a citation is supposed to prevent.

## Decision

Two layers, and the second is not another model:

1. **Similarity** against the source passages, catching fabrication and negation.
2. **Exact numeric containment.** Every figure in a claim must appear in some source.
   A single unmatched number disqualifies the sentence regardless of how well it scores.

Numbers are checked against *all* sources rather than only the best match, because a claim
may legitimately combine a figure from one passage with context from another.

With both layers, all seven cases above are classified correctly.

## Consequences

The reported unit is a per-sentence score with its best-matching source and the list of
unmatched figures — not a verdict. A boolean would hide the failure mode neither layer can
see.

**What remains uncaught**, stated plainly and pinned by a test that fails if it improves:
semantic substitution that negates no verb and changes no figure. "Managers approve
expenses" versus "directors approve expenses" scores as supported. That needs entailment —
an NLI head or an LLM judge — and until one is wired in, this verifier catches fabrication,
negation and numeric drift, and nothing else.

The verifier runs on every generated answer in `gatekeeper ask`. It is **not yet exercised
end to end**: answer generation requires an API key, which this project does not have, so
the verifier has only been tested against hand-written answers. The layers themselves are
measured; the integration is not.
