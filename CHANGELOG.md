# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are the phase tags from
[PROJECT_PLAN.md](PROJECT_PLAN.md).

Entries record **what was measured and what was disproved**, not only what was added. In a
project whose claims are numbers, a release note that omits the negative results is a
marketing document.

---

## [Unreleased]

### Deferred
- Contextual retrieval (Phase 3). Costed at roughly $37 via the Anthropic Batch API; not run.
- Agentic multi-hop planner with budgets (Phase 4). Each hop would be a fresh authorized query, so the model extends — untested, therefore unclaimed.

### Security
- **No RLS-bypassing credential in the request path.** `admin_session()` was documented "ingestion and migrations only" and had four request-path callers — including `load_principal`, which runs on every request. The API process therefore held `GK_DATABASE_OWNER_URL` and used it. Each privilege is now a `SECURITY DEFINER` function with a narrow return type, or a scoped grant to the app role ([ADR 0017](docs/adr/0017-no-owner-credential-in-the-request-path.md), migrations 0012–0014).
- The definer functions are owned by `gatekeeper_definer` — `NOLOGIN`, non-superuser, `BYPASSRLS`, `SELECT` on three tables — rather than by the superuser that ran the migration.
- Verified by pointing `database_owner_url` at a host that does not exist and exercising the request path, with a guard test asserting that fixture actually breaks `admin_session`. Reading the code had missed the `load_principal` caller twice.

### Known gaps
- No token revocation. A token is valid until it expires.
- A SQL-injection bug in the request path could read `query_cache.query_text` — a deliberate trade for removing the owner credential. See [THREAT_MODEL.md](docs/THREAT_MODEL.md) A3.
- Code execution in the API process is still bounded only to the principal being served, not to nothing.
- No rate limiting or query cost budget.
- MinIO blob storage is not RLS-governed. Nothing in the read path consults it.

---

## [v0.5] — Phase 5: Platform

### Added
- **Bearer-token authentication.** HS256 for local development, RS256 against a JWKS endpoint for OIDC. Signature, issuer, **audience** and expiry all checked.
- **Background ingestion** over arq (`make worker`, `gatekeeper jobs list|show|retry`, `index build --background`). Per-document transactions; dead-letter queue as a `failures` JSONB column; targeted retry.
- **OpenTelemetry tracing**, off unless `GK_OTEL_ENDPOINT` is set. Jaeger under `docker compose --profile observability`.
- **Concurrency sweep** (`make load`, [docs/LOAD.md](docs/LOAD.md)) with an embedding-bypass control arm.
- `gatekeeper.admin` claim, distinct from `gatekeeper.impersonate`; `/api/jobs`.

### Changed
- `IngestReport` counts `missing_sources` separately from `documents_skipped`.
- `search()` and `pipeline.retrieve()` are thin tracing wrappers around `_search`/`_retrieve`.

### Fixed
- **A background job reported `succeeded` while silently failing to index a document.** `index_one` returned the same value for "unchanged, nothing to do" and "the source file is gone from the clone". Found by hiding a source file and watching the job pass.

### Measured
- RLS-filtered ANN sustains **1,207 q/s** at concurrency 64 over 73,801 chunks with auditing on. The full request path caps at **201 q/s**.
- **The bottleneck is the co-located ONNX embedder, not row-level security** — a six-fold gap. The control arm is permanent so the comparison is never available without it.
- The `count_withheld` baseline costs **18 ms against 10 ms** for the authorized query it is compared against. Found by reading the first trace, not the code.

### Disproved
- **Turning the audit chain off makes tail latency worse, not better.** Throughput rose 45%; p99 went from 361 ms to 992 ms. The per-tenant advisory lock was pacing the pipeline; without it, requests pile onto the connection pool and the tail spreads.

### Substituted
Local-only constraints, not scope cuts: vanilla-HTML console rather than Next.js; Jaeger rather than Grafana + Langfuse; a Python async driver rather than k6 (not installed).

ADRs [0013](docs/adr/0013-tokens-assert-identity-not-entitlement.md)–[0016](docs/adr/0016-the-bottleneck-is-the-embedder.md).

---

## [v0.4] — Phase 4: Agentic and MCP

### Added
- **MCP server** over stdio with three tools, using the same query path as the CLI — so the red-team suite's guarantees cover it without re-testing. One principal per process, bound at launch.
- **Indirect prompt-injection detection**, scored at ingest and re-scorable in 15 seconds without re-embedding (`make rescan`).
- **Red team v2** (`make redteam-indirect`): payloads planted as readable documents in the live corpus, attacked through the real pipeline, removed in a `finally` block.
- **Query cache keyed by entitlement**, storing chunk ids rather than content.
- **Groundedness verification** in two layers.

### Measured
- Injection detection: 15/19 payloads caught at a **0.004% false-positive rate** (3 chunks in 73,801).
- Containment: of 19 planted payloads, **13 reached the model, 0 widened access — including 2 the classifier missed entirely.** That last clause is the argument; if containment only held where detection worked, security would rest on regular expressions.
- Cache hit: **267 ms → 12 ms** (21.7×).

### Disproved
- **The semantic layer of the injection classifier contributed nothing** and was deleted. The measurement shows it could not have worked at any threshold.
- **Embedding similarity cannot see numbers.** Changing an expense limit from 75 to 750 USD scores 0.867 against a faithful restatement's 0.884 — statistically indistinguishable, in a corpus that is nothing but thresholds. Negation, which this module was written expecting to be the blind spot, *is* caught (0.673).

### Fixed
- Injection classifier stuck at 21% detection: the threshold was miscalibrated against the saturation curve; every rule used `[^.\n]` so wrapped lines defeated four of them; `authoriz\w+` missed British "authorisation"; one rule caused 96% of false positives (164 → 3).
- Groundedness scored 0% on the first real generated answer — citation markers `[2]` were parsed as figures, and Markdown lists collapsed into one sentence.

ADRs [0009](docs/adr/0009-mcp-surface-and-one-principal-per-process.md)–[0012](docs/adr/0012-groundedness-needs-two-layers.md).

---

## [v0.3] — Phase 3: Retrieval quality

### Added
- **58-question golden set**, hand-written by reading the corpus — never generated from chunk text, which would make the eval circular and hand lexical search a win by construction. Each question is labelled with the principal entitled to the answer.
- **Stage-by-stage ablation** whose arms differ only in a `RetrievalConfig`, never in a code path.
- Hybrid search (dense + lexical, RRF) and cross-encoder reranking.

### Fixed
- **The selectivity cliff: 79 ms → 2.0 ms** for the most restricted principal, who had silently been the slowest — the users with the least access were paying the most latency.
- **A performance fix caused a correctness regression.** Retrieval returned zero results for a principal who could plainly `SELECT` two matching rows: the planner switched to HNSW where it had been sequentially scanning, and the fix needed an explicit `tenant_id` predicate.

### Measured
- nDCG@10 **0.793** (`dense+rerank`); Recall@10 vs exact brute force **1.000** at `ef_search=200`.
- Candidate pool 20 is the knee: 50 costs 2× the latency for no measurable gain, and 10 is consistently worse.

### Disproved
- **Hybrid search does not pay on this corpus.** indistinguishable from `dense+rerank` across four runs — ahead twice, behind twice — for 66% more latency. Built, measured, kept in the ablation, off by default.
- **13× of the cliff's 40× improvement came from a change made for an unrelated reason**, and this ADR's own proposed fix was on the wrong axis.
- A benchmark reporting recall 1.000 everywhere was a **measurement bug**: prepared-statement plan reuse across a planner GUC change, plus buffer-pool eviction. Fixed with `prepared_statement_cache_size=0` and a warm-up pass.

ADRs [0006](docs/adr/0006-filtered-ann-and-index-selectivity.md) (resolved), [0008](docs/adr/0008-hybrid-search-measured-and-disabled.md).

---

## [v0.2] — Phase 2: Authorization core

### Added
- **ABAC engine**: groups, clearance, need-to-know (subset semantics), jurisdiction, expiry, deny-overrides-allow — compiled into one SQL decision function.
- **Deny rules as data** in a `policies` table, read by both the database and the independent oracle.
- Chunk-level ACL overrides.
- **Hash-chained audit log** with per-tenant advisory locks.
- **Independent Python oracle** implementing the same written spec, reconciled against the database.
- Filtered-ANN benchmark with exact brute-force ground truth.

### Measured
- **0 disagreements across 442,782 (principal, chunk) pairs.**
- 0 leaks across 360 adversarial probes, 8 categories, plus direct primary-key fetch and aggregate enumeration.
- Over-block rate **1.15%**.
- `ef_search` raised to 200: the data showed 100 costs up to 20% recall.

### Fixed
- **The withheld count was compared against every tenant**, not the principal's own — an information leak that disclosed other corpora existed.

ADRs [0005](docs/adr/0005-one-decision-function-and-deny-as-data.md)–[0007](docs/adr/0007-explicit-claims-not-ambient-session-state.md).

---

## [v0.1] — Phase 1: Baseline RAG

### Added
- Structure-aware Markdown chunking: heading hierarchy prefixed onto every chunk, tables and code fences atomic, sentence-boundary splits for prose.
- Two embedding backends behind one interface: `bge-small-en-v1.5` via ONNX on CPU (default, no API key) and OpenAI `text-embedding-3-*`. Chunk size derived from the backend's context window.
- Dense retrieval with the ACL predicate **inside** the vector scan.
- Cited answer generation with hallucinated-citation rejection.
- Content-addressed blob storage; incremental reindex keyed on content hash; `index repair`.
- Demo console for side-by-side retrieval across principals.

### Fixed
- **A 45,343-token chunk was silently truncated.** A raw HTML table offered no sentence boundary, so every split rule declined. Added a last-resort hard split, and redefined `oversized` as "split without a semantic boundary" rather than "large".
- Blob upload failures no longer abort a batch — a MinIO clock skew cost a full run once.

ADR [0004](docs/adr/0004-embedding-columns-on-chunks.md).

---

## [v0.0] — Phase 0: Foundations

### Added
- Postgres 16 + pgvector 0.8, Redis, MinIO under one compose file. Alembic, domain models, ruff/mypy/pytest.
- **Row-level security `ENABLE`d and `FORCE`d** on every tenant-scoped table.
- **Split-privilege connection model**: migrations and ingestion as the table owner; queries as `gatekeeper_app`, a `NOSUPERUSER`/`NOBYPASSRLS` role.
- Claims pushed into a **transaction-local** GUC via `set_config(..., true)`, so authorization context cannot leak across pooled connections.
- ACLs derived by a declarative rule file: 19 rules, first match wins.

### Changed from the plan
- **The synthetic corpus generator was dropped** in favour of the GitLab Handbook plus a declarative ACL derivation layer. Real structure beats generated structure, and the rules file is a better artifact than a generator script.
- **RLS moved forward from Phase 2.** Retrofitting it onto an existing data-access layer means touching every query path, so the plumbing ships in the first two migrations.

ADRs [0001](docs/adr/0001-postgres-as-the-only-datastore.md)–[0003](docs/adr/0003-abac-over-rbac.md).


[Unreleased]: https://github.com/glibshchur/gatekeeper-rag/compare/v0.5...HEAD
[v0.5]: https://github.com/glibshchur/gatekeeper-rag/compare/v0.4...v0.5
[v0.4]: https://github.com/glibshchur/gatekeeper-rag/compare/v0.3...v0.4
[v0.3]: https://github.com/glibshchur/gatekeeper-rag/compare/v0.2...v0.3
[v0.2]: https://github.com/glibshchur/gatekeeper-rag/compare/v0.1...v0.2
[v0.1]: https://github.com/glibshchur/gatekeeper-rag/compare/v0.0...v0.1
[v0.0]: https://github.com/glibshchur/gatekeeper-rag/releases/tag/v0.0
