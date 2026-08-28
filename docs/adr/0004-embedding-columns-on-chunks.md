# 0004 — Embeddings are columns on `chunks`, one per vector space

**Status:** Accepted · **Date:** 2026-08-28 · **Phase:** 1

## Context

Embeddings have to live somewhere, and the obvious shape is a join table:
`chunk_embeddings(chunk_id, model, embedding)`. One row per chunk per model, no schema
change when a model is added, no wasted NULL columns.

Two things make that shape wrong here.

**Authorization.** The RLS policy is on `chunks`. If vectors live in a separate relation,
the ANN scan happens there and the authorization predicate is applied afterwards, when
the planner joins back. That is post-hoc filtering with extra steps — precisely the
architecture ADR 0002 exists to reject. The vector index and the policy have to be on the
same relation for the predicate to be part of the scan.

**Dimension is a type, not a value.** pgvector needs a declared dimension to build an
index. A single `embedding` column cannot hold both a 384-dimensional and a
1536-dimensional vector, so the join table would need a column per dimension anyway — the
same denormalisation, one relation further from the policy.

## Decision

`chunks` carries one nullable column per vector space (`embedding_384`,
`embedding_1536`), plus `embedding_model` recording which backend produced the row. Each
column has its own partial HNSW index over `WHERE embedding_N IS NOT NULL`.

Storage is `halfvec` (16-bit floats) with `halfvec_cosine_ops`.

## Consequences

**What this buys.** The authorization predicate and the similarity ordering are evaluated
by one scan over one relation. Two embedding spaces can be populated simultaneously over
the same corpus, which is a hard requirement for the Phase 3 ablation — comparing local
ONNX against hosted embeddings is only meaningful on identical chunks. The partial index
keeps each graph proportional to what is actually embedded rather than to the table, so
indexing a corpus in one space costs nothing in the other.

`halfvec` halves index and heap size. For normalised embeddings the recall cost is
negligible; at 25k chunks it is 45 MB against 89 MB, and the ratio holds as the corpus
grows.

**What this costs.** Adding a third embedding dimension is a migration, not an insert.
Chunks embedded in only one space carry a NULL column, which is cheap but untidy. And
`embedding_model` on the chunk row means re-embedding in a different space produces
*duplicate chunk rows* for the same document — the pipeline scopes its delete by
`embedding_model` for exactly this reason, and a query that forgets to filter on it would
return each chunk once per space.

The real cost is unresolved and belongs to Phase 2: HNSW returns `ef_search` candidates,
RLS then removes the unauthorized ones, and top-k comes back short. Phase 1 papers over
this with `hnsw.iterative_scan = 'relaxed_order'`, which makes pgvector keep pulling from
the graph until k *visible* rows are found. That is a mitigation, not a measurement. The
recall it costs is unknown until Phase 2 benchmarks it.

## Alternatives considered

- **`chunk_embeddings` join table.** Rejected above: it separates the policy from the scan.
- **One table per embedding space, each with its own RLS policy.** Keeps policy and scan
  together and avoids NULL columns. Rejected because the ACL would then be denormalised
  three times instead of twice, and every policy change would have to be applied to N
  tables in lockstep — a class of drift bug worth avoiding for a purely cosmetic gain.
- **Full-precision `vector` instead of `halfvec`.** Rejected on size; revisit if the
  Phase 3 ablation shows measurable recall loss attributable to quantisation.
