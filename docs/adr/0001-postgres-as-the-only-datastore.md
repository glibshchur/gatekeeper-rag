# 0001 — Postgres is the only datastore

**Status:** Accepted · **Date:** 2026-08-28 · **Phase:** 0

## Context

A retrieval system needs, at minimum: dense vector search, lexical search, document
metadata, and access-control state. The default industry answer is a dedicated vector
database (Pinecone, Weaviate, Qdrant) alongside a relational database for everything
else, and often a search cluster for BM25.

That split has a specific consequence for this project. If vectors live in one system and
ACLs in another, authorization has to be enforced *between* them — retrieve from the
vector store, then filter in application code against the relational store. That is
precisely the architecture this project exists to argue against. Every leak in a RAG
system with post-hoc filtering is a bug in the glue between two datastores.

## Decision

Postgres 16 with pgvector holds everything: embeddings (`halfvec`, HNSW), full-text
search (`tsvector`), document metadata, principals, policies, and the audit log.

## Consequences

**What this buys.** Authorization becomes a predicate in the same query as the vector
scan, evaluated by the same engine, under the same transaction. There is no window
between "retrieved" and "filtered" for a bug to live in. Hybrid search is a join, not a
network hop. Transactional consistency between a document's content and its ACL is free.
The whole system is one `docker compose up`, which is a hard requirement for a project
that must be runnable by a stranger without cloud credentials.

**What this costs.** Postgres will not match a purpose-built vector database on raw ANN
throughput at very large scale. Filtered ANN is harder here than in engines with
first-class filter support — the HNSW index scans, RLS removes rows, and top-k comes back
short. That problem is real and Phase 2 addresses it directly with partial indexes and
pgvector 0.8 `iterative_scan`; the recall cost will be measured and published rather than
hidden.

**When to revisit.** If a tenant exceeds roughly 10M chunks and p95 latency degrades past
budget with iterative scan tuned. At that point the correct move is probably sharding
Postgres, not adopting a second datastore, because the authorization argument above does
not weaken with scale.

## Alternatives considered

- **pgvector + a separate OpenSearch cluster for BM25.** Better lexical search quality.
  Rejected for Phase 0–3: it reintroduces the two-datastore authorization problem for the
  sparse half of hybrid retrieval. Revisit if `tsvector` ranking proves inadequate in the
  Phase 3 ablation.
- **Qdrant with payload-based filtering.** Genuinely good filtered-ANN support. Rejected
  because ACL state would then live in two places with no transactional link between
  them, and because "the database refuses to return the row" is a stronger and more
  auditable claim than "the vector store was asked to filter."
