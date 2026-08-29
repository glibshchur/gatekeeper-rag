# Filtered-ANN benchmark

Recall@10 of the HNSW path against exact brute-force ground truth, both
computed under the same row-level security policy, on a corpus of
73,801 chunks (bge-small-en-v1.5, 384d, halfvec, m=16, ef_construction=64).

**Selectivity** is the fraction of the corpus the principal may read. It is the
variable that matters: the more a policy filters, the more of the HNSW candidate
list is discarded before it can be ranked.

## Findings

**1. The selectivity cliff is fixed, and mostly not by the thing built to fix it.**

Phase 2 measured a 79x latency cliff: at 5.2% selectivity Postgres chose a parallel
sequential scan, at 84.6% it used the HNSW index. The access policy, not the query,
decided which. Recall stayed at 1.000 throughout -- a sequential scan is exact -- so
nothing in the results hinted that anything had changed.

Isolating the cause by downgrading and re-running against the same corpus:

| `authorize()` form | guest plan | guest p50 | raj p50 |
|---|---|---:|---:|
| Phase 2: `STABLE`, called per row | `Parallel Seq Scan` | 79.0 ms | 2.2 ms |
| ADR 0007: `IMMUTABLE`, inlinable | `HNSW (full)` | 6.1 ms | 2.2 ms |
| + coarse predicate & partial index | `HNSW (public partial)` | 2.0 ms | 2.2 ms |

**13x of the 40x came from ADR 0007**, which was written to fix lexical-query latency
and had nothing to do with this. An opaque `STABLE` function gives the planner a
default selectivity guess that made the sequential scan look cheap; making the
predicate `IMMUTABLE` and inlinable let it fold the clauses into the query's quals and
estimate them properly. The cliff was a symptom of the same root cause as the 11x
lexical slowdown, and neither diagnosis saw that at the time.

The remaining 3x is this phase's work: `coarse_predicate()` restates two of the
policy's own clauses in the query so the planner can index them, and migration 0008
adds partial HNSW graphs for the `public` and `public+internal` tiers. The public
graph is 4.4 MB against 81 MB for the full one, so the most restricted principal now
searches ~5% of the structure. Principals above ~80% selectivity are unaffected,
which is the intended outcome -- they were never on the wrong side of the cliff.

Worth stating plainly: ADR 0006 proposed **per-tenant** partial indexes. That was the
wrong axis. The cliff is intra-tenant -- `guest` and `mira` share a tenant and differ
18x in what they can read -- so a per-tenant index would have helped neither.

**2. `iterative_scan` fixes short returns; it does not fix recall.** At `ef_search=40`
with iterative scan `off`, up to 3 of 10 queries return fewer than k rows. Turning it
on eliminates short returns entirely -- which is what Phase 1 observed and wrongly
read as sufficient. Recall at that setting is still 0.79-0.94: the results look
complete and are quietly wrong. `ef_search >= 200` reaches 1.000 for every principal
at a cost of ~1 ms.

**3. Where the index is used, it is worth 60x.** 4-6 ms against 280-330 ms for exact
brute force over the same 73,797 chunks under the same policy.

## Full sweep

| principal | selectivity | iterative_scan | ef_search | recall@10 | short | p50 | p95 | exact p50 |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| guest | 5.2% | `off` | 40 | 0.940 | 0 | 4 ms | 4 ms | 64 ms |
| guest | 5.2% | `off` | 100 | 0.990 | 0 | 4 ms | 4 ms | 64 ms |
| guest | 5.2% | `off` | 200 | 1.000 | 0 | 5 ms | 5 ms | 64 ms |
| guest | 5.2% | `off` | 400 | 1.000 | 0 | 5 ms | 6 ms | 64 ms |
| guest | 5.2% | `relaxed_order` | 40 | 0.940 | 0 | 4 ms | 4 ms | 64 ms |
| guest | 5.2% | `relaxed_order` | 100 | 0.990 | 0 | 4 ms | 4 ms | 64 ms |
| guest | 5.2% | `relaxed_order` | 200 | 1.000 | 0 | 4 ms | 5 ms | 64 ms |
| guest | 5.2% | `relaxed_order` | 400 | 1.000 | 0 | 5 ms | 5 ms | 64 ms |
| guest | 5.2% | `strict_order` | 40 | 0.940 | 0 | 4 ms | 5 ms | 64 ms |
| guest | 5.2% | `strict_order` | 100 | 0.990 | 0 | 5 ms | 8 ms | 64 ms |
| guest | 5.2% | `strict_order` | 200 | 1.000 | 0 | 6 ms | 11 ms | 64 ms |
| guest | 5.2% | `strict_order` | 400 | 1.000 | 0 | 6 ms | 8 ms | 64 ms |
| raj | 84.7% | `off` | 40 | 0.800 | 3 | 4 ms | 5 ms | 75 ms |
| raj | 84.7% | `off` | 100 | 0.900 | 1 | 4 ms | 5 ms | 75 ms |
| raj | 84.7% | `off` | 200 | 0.990 | 0 | 5 ms | 6 ms | 75 ms |
| raj | 84.7% | `off` | 400 | 1.000 | 0 | 7 ms | 7 ms | 75 ms |
| raj | 84.7% | `relaxed_order` | 40 | 0.940 | 0 | 4 ms | 5 ms | 75 ms |
| raj | 84.7% | `relaxed_order` | 100 | 0.960 | 0 | 5 ms | 6 ms | 75 ms |
| raj | 84.7% | `relaxed_order` | 200 | 0.990 | 0 | 6 ms | 9 ms | 75 ms |
| raj | 84.7% | `relaxed_order` | 400 | 1.000 | 0 | 6 ms | 7 ms | 75 ms |
| raj | 84.7% | `strict_order` | 40 | 0.930 | 0 | 4 ms | 5 ms | 75 ms |
| raj | 84.7% | `strict_order` | 100 | 0.940 | 0 | 5 ms | 5 ms | 75 ms |
| raj | 84.7% | `strict_order` | 200 | 0.990 | 0 | 6 ms | 6 ms | 75 ms |
| raj | 84.7% | `strict_order` | 400 | 1.000 | 0 | 6 ms | 8 ms | 75 ms |
| sam | 87.3% | `off` | 40 | 0.800 | 3 | 4 ms | 4 ms | 75 ms |
| sam | 87.3% | `off` | 100 | 0.900 | 1 | 5 ms | 5 ms | 75 ms |
| sam | 87.3% | `off` | 200 | 0.990 | 0 | 5 ms | 6 ms | 75 ms |
| sam | 87.3% | `off` | 400 | 1.000 | 0 | 6 ms | 7 ms | 75 ms |
| sam | 87.3% | `relaxed_order` | 40 | 0.940 | 0 | 5 ms | 5 ms | 75 ms |
| sam | 87.3% | `relaxed_order` | 100 | 0.960 | 0 | 5 ms | 5 ms | 75 ms |
| sam | 87.3% | `relaxed_order` | 200 | 0.990 | 0 | 6 ms | 6 ms | 75 ms |
| sam | 87.3% | `relaxed_order` | 400 | 1.000 | 0 | 8 ms | 8 ms | 75 ms |
| sam | 87.3% | `strict_order` | 40 | 0.930 | 0 | 5 ms | 6 ms | 75 ms |
| sam | 87.3% | `strict_order` | 100 | 0.940 | 0 | 5 ms | 7 ms | 75 ms |
| sam | 87.3% | `strict_order` | 200 | 0.990 | 0 | 7 ms | 13 ms | 75 ms |
| sam | 87.3% | `strict_order` | 400 | 1.000 | 0 | 6 ms | 8 ms | 75 ms |
| dana | 83.0% | `off` | 40 | 0.920 | 1 | 5 ms | 7 ms | 76 ms |
| dana | 83.0% | `off` | 100 | 0.970 | 0 | 5 ms | 7 ms | 76 ms |
| dana | 83.0% | `off` | 200 | 1.000 | 0 | 5 ms | 6 ms | 76 ms |
| dana | 83.0% | `off` | 400 | 1.000 | 0 | 7 ms | 8 ms | 76 ms |
| dana | 83.0% | `relaxed_order` | 40 | 0.940 | 0 | 5 ms | 5 ms | 76 ms |
| dana | 83.0% | `relaxed_order` | 100 | 0.970 | 0 | 5 ms | 5 ms | 76 ms |
| dana | 83.0% | `relaxed_order` | 200 | 1.000 | 0 | 6 ms | 7 ms | 76 ms |
| dana | 83.0% | `relaxed_order` | 400 | 1.000 | 0 | 7 ms | 8 ms | 76 ms |
| dana | 83.0% | `strict_order` | 40 | 0.940 | 0 | 5 ms | 5 ms | 76 ms |
| dana | 83.0% | `strict_order` | 100 | 0.970 | 0 | 6 ms | 6 ms | 76 ms |
| dana | 83.0% | `strict_order` | 200 | 1.000 | 0 | 6 ms | 6 ms | 76 ms |
| dana | 83.0% | `strict_order` | 400 | 1.000 | 0 | 7 ms | 9 ms | 76 ms |
| mira | 90.2% | `off` | 40 | 0.970 | 0 | 4 ms | 5 ms | 76 ms |
| mira | 90.2% | `off` | 100 | 0.990 | 0 | 5 ms | 5 ms | 76 ms |
| mira | 90.2% | `off` | 200 | 1.000 | 0 | 5 ms | 6 ms | 76 ms |
| mira | 90.2% | `off` | 400 | 1.000 | 0 | 6 ms | 7 ms | 76 ms |
| mira | 90.2% | `relaxed_order` | 40 | 0.970 | 0 | 4 ms | 4 ms | 76 ms |
| mira | 90.2% | `relaxed_order` | 100 | 0.990 | 0 | 4 ms | 4 ms | 76 ms |
| mira | 90.2% | `relaxed_order` | 200 | 1.000 | 0 | 5 ms | 5 ms | 76 ms |
| mira | 90.2% | `relaxed_order` | 400 | 1.000 | 0 | 6 ms | 7 ms | 76 ms |
| mira | 90.2% | `strict_order` | 40 | 0.970 | 0 | 5 ms | 5 ms | 76 ms |
| mira | 90.2% | `strict_order` | 100 | 0.990 | 0 | 5 ms | 6 ms | 76 ms |
| mira | 90.2% | `strict_order` | 200 | 1.000 | 0 | 6 ms | 6 ms | 76 ms |
| mira | 90.2% | `strict_order` | 400 | 1.000 | 0 | 7 ms | 8 ms | 76 ms |
| pilar | 84.6% | `off` | 40 | 0.800 | 3 | 4 ms | 4 ms | 76 ms |
| pilar | 84.6% | `off` | 100 | 0.900 | 1 | 4 ms | 4 ms | 76 ms |
| pilar | 84.6% | `off` | 200 | 0.990 | 0 | 4 ms | 5 ms | 76 ms |
| pilar | 84.6% | `off` | 400 | 1.000 | 0 | 6 ms | 9 ms | 76 ms |
| pilar | 84.6% | `relaxed_order` | 40 | 0.940 | 0 | 4 ms | 6 ms | 76 ms |
| pilar | 84.6% | `relaxed_order` | 100 | 0.960 | 0 | 4 ms | 5 ms | 76 ms |
| pilar | 84.6% | `relaxed_order` | 200 | 0.990 | 0 | 5 ms | 5 ms | 76 ms |
| pilar | 84.6% | `relaxed_order` | 400 | 1.000 | 0 | 6 ms | 7 ms | 76 ms |
| pilar | 84.6% | `strict_order` | 40 | 0.930 | 0 | 4 ms | 4 ms | 76 ms |
| pilar | 84.6% | `strict_order` | 100 | 0.940 | 0 | 4 ms | 5 ms | 76 ms |
| pilar | 84.6% | `strict_order` | 200 | 0.990 | 0 | 5 ms | 5 ms | 76 ms |
| pilar | 84.6% | `strict_order` | 400 | 1.000 | 0 | 6 ms | 7 ms | 76 ms |

## The selectivity cliff, before and after

`coarse_predicate()` restates two of the RLS policy's own clauses in the query —
`min_clearance <= clearance` and `sensitivity = 'public' OR allowed_groups && groups`
— so the planner can estimate and index them, and migration 0008 adds partial HNSW
graphs for the `public` and `public+internal` tiers those clauses can match.

Both columns are the same principal, corpus, queries and settings
(`relaxed_order`, `ef_search=200`), measured in a single run. `recall` is
against exact brute-force ground truth computed under the same policy.

| principal | selectivity | before: plan / p50 / recall@10 | after: plan / p50 / recall@10 |
|---|---:|---|---|
| guest | 5.2% | `HNSW (full)` · 8 ms · 0.930 | `HNSW (public partial)` · 4 ms · 1.000 |
| raj | 84.7% | `HNSW (full)` · 5 ms · 0.990 | `HNSW (full)` · 6 ms · 0.990 |
| sam | 87.3% | `HNSW (full)` · 5 ms · 0.990 | `HNSW (full)` · 6 ms · 0.990 |
| dana | 83.0% | `HNSW (full)` · 6 ms · 1.000 | `HNSW (full)` · 6 ms · 1.000 |
| mira | 90.2% | `HNSW (full)` · 5 ms · 1.000 | `HNSW (full)` · 5 ms · 1.000 |
| pilar | 84.6% | `HNSW (full)` · 8 ms · 0.990 | `HNSW (full)` · 5 ms · 0.990 |
