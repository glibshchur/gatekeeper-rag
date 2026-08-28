# Filtered-ANN benchmark

Recall@10 of the HNSW path against exact brute-force ground truth, both
computed under the same row-level security policy, on a corpus of
73,797 chunks (bge-small-en-v1.5, 384d, halfvec, m=16, ef_construction=64).

**Selectivity** is the fraction of the corpus the principal may read. It is the
variable that matters: the more a policy filters, the more of the HNSW candidate
list is discarded before it can be ranked.

## Findings

**1. Below a selectivity threshold the planner abandons the vector index.** This is
the headline, and it is not a tuning problem. At 5.2% selectivity Postgres estimates
the filter will leave ~1 row and chooses a parallel sequential scan; at 84.6% it uses
the HNSW index. Confirmed by `EXPLAIN ANALYZE`, not inferred from timings:

| principal | selectivity | plan | time |
|---|---:|---|---:|
| guest | 5.2% | `Parallel Seq Scan on chunks` | 69 ms |
| raj | 84.6% | `Index Scan using ix_chunks_embedding_384_hnsw` | 0.9 ms |

A 79x latency cliff, triggered by the access policy rather than by the query, with
no error and no warning. Recall stays at 1.000 the whole way down -- a sequential
scan is exact -- so nothing in the results hints that anything changed. Any tenant
whose users are tightly scoped falls off this cliff and simply runs slowly forever.

The fix is to make the filter something the index can exploit rather than something
applied to its output: partial HNSW indexes per tenant, and per high-cardinality
group, so a restrictive principal searches a small index instead of filtering a
large one. That is Phase 3 work; this benchmark exists to size it first.

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
| guest | 5.2% | `off` | 40 | 1.000 | 0 | 60 ms | 61 ms | 58 ms |
| guest | 5.2% | `off` | 100 | 1.000 | 0 | 59 ms | 60 ms | 58 ms |
| guest | 5.2% | `off` | 200 | 1.000 | 0 | 60 ms | 60 ms | 58 ms |
| guest | 5.2% | `off` | 400 | 1.000 | 0 | 58 ms | 60 ms | 58 ms |
| guest | 5.2% | `relaxed_order` | 40 | 1.000 | 0 | 57 ms | 59 ms | 58 ms |
| guest | 5.2% | `relaxed_order` | 100 | 1.000 | 0 | 57 ms | 58 ms | 58 ms |
| guest | 5.2% | `relaxed_order` | 200 | 1.000 | 0 | 57 ms | 58 ms | 58 ms |
| guest | 5.2% | `relaxed_order` | 400 | 1.000 | 0 | 57 ms | 58 ms | 58 ms |
| guest | 5.2% | `strict_order` | 40 | 1.000 | 0 | 57 ms | 58 ms | 58 ms |
| guest | 5.2% | `strict_order` | 100 | 1.000 | 0 | 57 ms | 63 ms | 58 ms |
| guest | 5.2% | `strict_order` | 200 | 1.000 | 0 | 57 ms | 59 ms | 58 ms |
| guest | 5.2% | `strict_order` | 400 | 1.000 | 0 | 57 ms | 58 ms | 58 ms |
| raj | 84.6% | `off` | 40 | 0.790 | 3 | 4 ms | 5 ms | 282 ms |
| raj | 84.6% | `off` | 100 | 0.930 | 1 | 5 ms | 5 ms | 282 ms |
| raj | 84.6% | `off` | 200 | 1.000 | 0 | 5 ms | 6 ms | 282 ms |
| raj | 84.6% | `off` | 400 | 1.000 | 0 | 6 ms | 6 ms | 282 ms |
| raj | 84.6% | `relaxed_order` | 40 | 0.930 | 0 | 4 ms | 5 ms | 282 ms |
| raj | 84.6% | `relaxed_order` | 100 | 0.980 | 0 | 4 ms | 5 ms | 282 ms |
| raj | 84.6% | `relaxed_order` | 200 | 1.000 | 0 | 5 ms | 6 ms | 282 ms |
| raj | 84.6% | `relaxed_order` | 400 | 1.000 | 0 | 6 ms | 6 ms | 282 ms |
| raj | 84.6% | `strict_order` | 40 | 0.930 | 0 | 4 ms | 5 ms | 282 ms |
| raj | 84.6% | `strict_order` | 100 | 0.980 | 0 | 4 ms | 5 ms | 282 ms |
| raj | 84.6% | `strict_order` | 200 | 1.000 | 0 | 5 ms | 5 ms | 282 ms |
| raj | 84.6% | `strict_order` | 400 | 1.000 | 0 | 6 ms | 6 ms | 282 ms |
| sam | 87.3% | `off` | 40 | 0.790 | 3 | 4 ms | 5 ms | 292 ms |
| sam | 87.3% | `off` | 100 | 0.930 | 1 | 4 ms | 5 ms | 292 ms |
| sam | 87.3% | `off` | 200 | 1.000 | 0 | 5 ms | 7 ms | 292 ms |
| sam | 87.3% | `off` | 400 | 1.000 | 0 | 6 ms | 6 ms | 292 ms |
| sam | 87.3% | `relaxed_order` | 40 | 0.930 | 0 | 4 ms | 5 ms | 292 ms |
| sam | 87.3% | `relaxed_order` | 100 | 0.980 | 0 | 4 ms | 5 ms | 292 ms |
| sam | 87.3% | `relaxed_order` | 200 | 1.000 | 0 | 5 ms | 5 ms | 292 ms |
| sam | 87.3% | `relaxed_order` | 400 | 1.000 | 0 | 6 ms | 6 ms | 292 ms |
| sam | 87.3% | `strict_order` | 40 | 0.930 | 0 | 4 ms | 5 ms | 292 ms |
| sam | 87.3% | `strict_order` | 100 | 0.980 | 0 | 4 ms | 5 ms | 292 ms |
| sam | 87.3% | `strict_order` | 200 | 1.000 | 0 | 5 ms | 6 ms | 292 ms |
| sam | 87.3% | `strict_order` | 400 | 1.000 | 0 | 6 ms | 6 ms | 292 ms |
| dana | 83.0% | `off` | 40 | 0.920 | 1 | 4 ms | 5 ms | 310 ms |
| dana | 83.0% | `off` | 100 | 0.990 | 0 | 5 ms | 5 ms | 310 ms |
| dana | 83.0% | `off` | 200 | 1.000 | 0 | 5 ms | 6 ms | 310 ms |
| dana | 83.0% | `off` | 400 | 1.000 | 0 | 6 ms | 6 ms | 310 ms |
| dana | 83.0% | `relaxed_order` | 40 | 0.940 | 0 | 4 ms | 4 ms | 310 ms |
| dana | 83.0% | `relaxed_order` | 100 | 0.990 | 0 | 4 ms | 5 ms | 310 ms |
| dana | 83.0% | `relaxed_order` | 200 | 1.000 | 0 | 5 ms | 5 ms | 310 ms |
| dana | 83.0% | `relaxed_order` | 400 | 1.000 | 0 | 6 ms | 6 ms | 310 ms |
| dana | 83.0% | `strict_order` | 40 | 0.940 | 0 | 4 ms | 5 ms | 310 ms |
| dana | 83.0% | `strict_order` | 100 | 0.990 | 0 | 5 ms | 5 ms | 310 ms |
| dana | 83.0% | `strict_order` | 200 | 1.000 | 0 | 5 ms | 5 ms | 310 ms |
| dana | 83.0% | `strict_order` | 400 | 1.000 | 0 | 6 ms | 7 ms | 310 ms |
| mira | 90.2% | `off` | 40 | 0.980 | 0 | 4 ms | 5 ms | 327 ms |
| mira | 90.2% | `off` | 100 | 1.000 | 0 | 4 ms | 5 ms | 327 ms |
| mira | 90.2% | `off` | 200 | 1.000 | 0 | 5 ms | 5 ms | 327 ms |
| mira | 90.2% | `off` | 400 | 1.000 | 0 | 6 ms | 6 ms | 327 ms |
| mira | 90.2% | `relaxed_order` | 40 | 0.980 | 0 | 4 ms | 4 ms | 327 ms |
| mira | 90.2% | `relaxed_order` | 100 | 1.000 | 0 | 4 ms | 5 ms | 327 ms |
| mira | 90.2% | `relaxed_order` | 200 | 1.000 | 0 | 5 ms | 5 ms | 327 ms |
| mira | 90.2% | `relaxed_order` | 400 | 1.000 | 0 | 6 ms | 7 ms | 327 ms |
| mira | 90.2% | `strict_order` | 40 | 0.980 | 0 | 4 ms | 4 ms | 327 ms |
| mira | 90.2% | `strict_order` | 100 | 1.000 | 0 | 4 ms | 6 ms | 327 ms |
| mira | 90.2% | `strict_order` | 200 | 1.000 | 0 | 5 ms | 6 ms | 327 ms |
| mira | 90.2% | `strict_order` | 400 | 1.000 | 0 | 6 ms | 7 ms | 327 ms |
| pilar | 84.6% | `off` | 40 | 0.790 | 3 | 4 ms | 4 ms | 283 ms |
| pilar | 84.6% | `off` | 100 | 0.930 | 1 | 4 ms | 5 ms | 283 ms |
| pilar | 84.6% | `off` | 200 | 1.000 | 0 | 5 ms | 6 ms | 283 ms |
| pilar | 84.6% | `off` | 400 | 1.000 | 0 | 6 ms | 6 ms | 283 ms |
| pilar | 84.6% | `relaxed_order` | 40 | 0.930 | 0 | 4 ms | 5 ms | 283 ms |
| pilar | 84.6% | `relaxed_order` | 100 | 0.980 | 0 | 4 ms | 6 ms | 283 ms |
| pilar | 84.6% | `relaxed_order` | 200 | 1.000 | 0 | 5 ms | 5 ms | 283 ms |
| pilar | 84.6% | `relaxed_order` | 400 | 1.000 | 0 | 6 ms | 6 ms | 283 ms |
| pilar | 84.6% | `strict_order` | 40 | 0.930 | 0 | 4 ms | 6 ms | 283 ms |
| pilar | 84.6% | `strict_order` | 100 | 0.980 | 0 | 4 ms | 5 ms | 283 ms |
| pilar | 84.6% | `strict_order` | 200 | 1.000 | 0 | 5 ms | 5 ms | 283 ms |
| pilar | 84.6% | `strict_order` | 400 | 1.000 | 0 | 6 ms | 6 ms | 283 ms |
