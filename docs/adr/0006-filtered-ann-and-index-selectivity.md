# 0006 — Filtered ANN: the access predicate can cost you the vector index

**Status:** Resolved in Phase 3 — see [Resolution](#resolution) · **Date:** 2026-08-28 · **Phase:** 2

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

---

## Resolution

**Date:** 2026-08-29 · **Phase:** 3

The cliff is gone: 79 ms → 2.0 ms for the most restricted principal, a 40x improvement.
Two things worth recording, because neither is what this ADR predicted.

**The proposed fix was on the wrong axis.** This record proposed *per-tenant* partial
indexes. The cliff is **intra**-tenant — `guest` and `mira` share a tenant and differ 18x
in what they can read — so a per-tenant index would have helped neither of them. The axis
that works is `sensitivity`: four values, present in the policy verbatim, and correlated
with selectivity because the most restricted principals are exactly the ones limited to
`public`.

**Most of the fix was an accident.** Isolated by downgrading `authorize()` and re-running
against the same corpus and queries:

| `authorize()` form | guest plan | guest p50 | raj p50 |
|---|---|---:|---:|
| Phase 2: `STABLE`, called per row | `Parallel Seq Scan` | 79.0 ms | 2.2 ms |
| [ADR 0007](0007-explicit-claims-not-ambient-session-state.md): `IMMUTABLE`, inlinable | `HNSW (full)` | 6.1 ms | 2.2 ms |
| + `coarse_predicate()` & partial index (0008) | `HNSW (public partial)` | **2.0 ms** | 2.2 ms |

**13x of the 40x came from ADR 0007**, which was written to fix an unrelated 11x slowdown
on lexical queries. An opaque `STABLE` function gives the planner a default selectivity
guess that made the sequential scan look cheap; making the predicate `IMMUTABLE` and
inlinable let it fold the clauses into the query's quals and estimate them properly. The
cliff and the lexical slowdown were the same root cause — a predicate the planner could
not see into — and neither diagnosis noticed that at the time.

The remaining 3x is deliberate: `coarse_predicate()` restates two of the policy's own
clauses in the query (`min_clearance <= clearance`, and `sensitivity = 'public' OR
allowed_groups && groups`) so the planner can index them, and migration 0008 adds partial
HNSW graphs for the `public` and `public+internal` tiers. The public graph is 4.4 MB
against 81 MB for the full one.

Recall is unaffected: 1.000 at the default `ef_search=200` for every principal, against
exact brute-force ground truth. Authorization is unaffected: 0 leaks and 442,806
(principal, chunk) pairs still reconciling against the independent oracle.
`test_the_coarse_predicate_removes_nothing_the_policy_permits` pins the superset property
directly, by comparing result sets with and without the coarse clauses rather than by
reasoning about which clauses are safe to restate.

**The lesson worth keeping.** Two separate performance investigations, months apart in
narrative time, turned out to share a root cause that neither had named. The thing that
connected them was measuring the *plan*, not the latency — ADR 0006's original diagnosis
was correct precisely because it ran `EXPLAIN` instead of inferring from timings, and this
resolution was possible because the benchmark now records the chosen plan on every row.
