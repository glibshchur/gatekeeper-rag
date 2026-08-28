# 0006 — Filtered ANN: the access predicate can cost you the vector index

**Status:** Accepted · **Date:** 2026-08-28 · **Phase:** 2

## Context

ADRs 0001 and 0004 both deferred the same question: what does row-level security cost
approximate nearest-neighbour search? Phase 1 turned on `hnsw.iterative_scan`, observed
no short returns, and moved on. That observation was true and almost worthless — it
answered the visible failure and said nothing about the invisible one.

[`docs/BENCHMARKS.md`](../BENCHMARKS.md) measures both, against exact brute-force ground
truth computed under the same policy.

## What the measurement found

**1. Below a selectivity threshold the planner abandons the index.** At 5.2% selectivity
Postgres estimates the ABAC predicate will leave about one row and chooses a parallel
sequential scan; at 84.6% it uses the HNSW index. Confirmed by `EXPLAIN ANALYZE`:

| principal | selectivity | plan | time |
|---|---:|---|---:|
| guest | 5.2% | `Parallel Seq Scan on chunks` | 69 ms |
| raj | 84.6% | `Index Scan using ix_chunks_embedding_384_hnsw` | 0.9 ms |

A 79x cliff, triggered by *who is asking* rather than by what they asked. Nothing in the
results indicates it: recall stays at 1.000, because a sequential scan is exact. A tenant
whose users are tightly scoped runs slowly forever and never sees an error.

**2. `iterative_scan` fixes short returns, not recall.** At `ef_search=40` with iterative
scan off, up to 3 of 10 queries return fewer than k rows. Turning it on eliminates that
entirely — and recall is still 0.79–0.94. The results look complete and are quietly
missing near neighbours. `ef_search >= 200` reaches 1.000 for every principal at a cost of
roughly 1 ms, which is why that is now the default in `retrieval/search.py`.

## Decision

Accept the cliff for now, document it, and make `ef_search=200` the default. Do not
attempt partial indexes in Phase 2.

The fix is to make the authorization filter something the index can *exploit* rather than
something applied to its output: partial HNSW indexes per tenant, and per
high-cardinality group, so a restrictive principal searches a small index instead of
filtering a large one. That is a schema and maintenance problem — index-per-group does not
scale to arbitrary group counts, and choosing which groups deserve an index needs
selectivity statistics this system does not yet collect. Sizing it was the point of the
benchmark; building it is Phase 3.

## Consequences

The most restricted users get the worst latency. That is exactly backwards from what an
enterprise deployment wants, and it is worth stating plainly rather than burying: the
guest-tier experience in this system is currently 60x slower than the executive one, for
reasons that have nothing to do with fairness and everything to do with query planning.

## A note on the measurement itself

Two bugs produced confident wrong numbers before this was written, and both are recorded
in the module docstring because either would have shipped as a finding:

- The exact and approximate queries were byte-identical SQL differing only in a planner
  GUC. Prepared-statement plans are not invalidated by GUC changes, so the sequential-scan
  plan prepared for ground truth was reused for every measurement. Latency matched to
  within noise and recall was a perfect 1.000 — both wrong, both plausible.
- Computing ground truth scans the whole table and evicts the index from
  `shared_buffers`, so the first timed measurements were cold reads.

Recall of 1.000 at every selectivity should have been suspicious on sight. A benchmark
that reports exactly what you expected is not evidence.
