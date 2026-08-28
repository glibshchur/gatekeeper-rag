# gatekeeper-rag

**A permission-aware enterprise RAG platform where authorization is enforced inside the
database, not in application code.**

Most internal-chatbot demos answer questions well and handle access control by filtering
results in Python after retrieval. That is the wrong layer: a bug, an injected prompt, or
a compromised API process leaks documents. `gatekeeper-rag` pushes authorization into
Postgres row-level security, so the database itself refuses to return rows the caller is
not cleared to see — and the query layer connects as a role that *cannot* bypass it.

> **Status: Phase 2 of 6.** ABAC engine, chunk-level ACLs, tamper-evident audit chain,
> adversarial test suite, and the filtered-ANN benchmark are in place. Hybrid search,
> reranking, and the retrieval evaluation harness land in Phase 3. See
> [PROJECT_PLAN.md](PROJECT_PLAN.md) for the full roadmap.

## Results

| | |
|---|---|
| Leaks across 360 adversarial probes + 6 direct-fetch + 8 boundary probes | **0** |
| (principal, chunk) pairs where the database and an independent oracle disagree | **0 of 442,782** |
| Over-block rate (entitled results withheld) | **1.15%** |
| Retrieval latency p50 / p95 | **11 ms / 24 ms** |
| Recall@10 vs exact brute force, `ef_search=200` | **1.000** |

The authorization numbers come from [`src/gatekeeper/redteam/`](src/gatekeeper/redteam/),
which scores the SQL policy against a **separate Python implementation of the same written
spec**. Asking the database whether the database got it right proves nothing; two
implementations disagreeing loudly is the point. Retrieval numbers are in
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md), measured against exact brute-force ground truth.

**The most useful thing that benchmark found is a problem, not a win:** below roughly 10%
selectivity the query planner abandons the HNSW index entirely and sequentially scans, so
the most tightly-scoped users get 79x worse latency — silently, with recall unaffected.
See [ADR 0006](docs/adr/0006-filtered-ann-and-index-selectivity.md).

---

## Quickstart

Requires Docker and [uv](https://docs.astral.sh/uv/). No API keys needed.

```bash
make install     # venv + dependencies
make bootstrap   # infra up, migrations, fetch corpus, seed with derived ACLs
make test-all    # includes the RLS isolation tests
```

`make bootstrap` clones the [GitLab Handbook](https://gitlab.com/gitlab-com/content-sites/handbook)
(4,586 Markdown files, MIT-licensed), overlays a plausible enterprise access model onto its
real departmental structure, chunks and embeds it locally, and loads it into Postgres.
Answer *generation* is the one feature that needs an API key; without one the CLI does
retrieval and says so.

## The console

```bash
make ui          # http://127.0.0.1:8077
```

Pick one or more principals, ask a question, and see the same query answered from
different corpora. Each column shows what that principal retrieved, how many results
authorization removed, and the sensitivity of every source.

The sharpest thing it makes visible: ask *"How do I report a security incident?"* as Sam
(security engineer, clearance 1) and Mira (CFO, clearance 3), and Sam gets a `restricted`
incident-response guide that Mira does not. **Clearance is a ceiling, not a key** — a high
clearance without the right group grants nothing.

> The console impersonates principals with no credential check. That is deliberate — it is
> what makes the comparison possible — and it is why it binds to loopback and says so in a
> banner. Real authentication is Phase 5.

## The demo

Same question, same corpus, same ranking function — two principals:

```console
$ make ask Q="What is the board meeting cadence and who attends?" WHO=raj
asked as Raj Mehta — Backend Engineer  clearance=1 groups=all-employees, engineering
5 sources in 58 ms · 1 withheld by authorization

  #  score  sensitivity  source
  1  0.701  public       Cadence — Overview > Quarter
  2  0.662  public       Cadence — Overview > Week
  ...

$ make ask Q="What is the board meeting cadence and who attends?" WHO=mira
asked as Mira Lindqvist — CFO  clearance=3 groups=all-employees, finance, executives
5 sources in 53 ms

  #  score  sensitivity  source
  1  0.701  public       Cadence — Overview > Quarter
  2  0.677  restricted   CEO — Why I'm at GitLab > CEO Meeting Cadence   ← only Mira
  ...
```

Raj is not filtered out of a list he was shown. The row never leaves Postgres.

## What Phase 2 delivers

- **ABAC engine in one SQL function.** `gatekeeper.authorize()` enforces tenant, expiry,
  deny rules, clearance, group overlap, need-to-know, and jurisdiction. Both policies call
  it, so `documents` and `chunks` cannot drift apart.
- **Need-to-know is subset, not overlap** — a chunk tagged `{pii, compensation}` requires
  both grants. **Jurisdiction** scopes per-entity employment policy by region.
  **Deny beats allow**, and deny rules live in a table as data, so a litigation hold is an
  `INSERT` rather than a deploy.
- **Chunk-level ACL overrides** that can only *tighten*: clearance and sensitivity take
  the maximum, tags union, groups intersect. A malformed override cannot grant access.
- **Hash-chained audit log**, written in the same transaction as the query it describes,
  append-only from the data plane, with tamper detection tested by editing and deleting
  rows.
- **Adversarial suite**: 360 probes across 8 attack categories, plus direct primary-key
  fetch, aggregate enumeration, forged expired claims, cross-tenant probing, and an
  exhaustive oracle reconciliation over every (principal, chunk) pair.
- **Filtered-ANN benchmark** with exact ground truth, and `ef_search` raised to 200
  because the data said 100 costs up to 20% recall.

## What Phase 1 delivers

- **Structure-aware Markdown chunking**: heading hierarchy preserved and prefixed onto
  every chunk, tables and code fences atomic, oversized tables split on row boundaries
  with the header repeated, sentence-boundary splits for prose.
- **Two embedding backends behind one interface**: `bge-small-en-v1.5` via ONNX Runtime on
  CPU (default, no API key) and OpenAI `text-embedding-3-*`. Chunk size is derived from
  the backend's context window, not hardcoded.
- **Dense retrieval with the ACL predicate inside the vector scan** — `halfvec` HNSW
  indexes live on the same relation as the RLS policy, so ranking and authorization are
  one scan. See [ADR 0004](docs/adr/0004-embedding-columns-on-chunks.md).
- **Cited answer generation** with source spotlighting, hallucinated-citation rejection,
  and an explicit ungrounded warning when the model cites nothing.
- Content-addressed blob storage in MinIO; incremental re-index keyed on content hash,
  and `index repair` to re-chunk only documents the current parameters invalidate.
- A **demo console** (`make ui`) for side-by-side retrieval across principals.

## What Phase 0 delivers

- Postgres 16 + pgvector 0.8 schema with **row-level security enabled and forced** on every
  tenant-scoped table.
- A **split-privilege connection model**: migrations and ingestion run as the table owner;
  queries run as `gatekeeper_app`, a `NOSUPERUSER`/`NOBYPASSRLS` role.
- `Principal` claims pushed into a **transaction-local** Postgres GUC via `set_config(...,
  true)`, so authorization context cannot leak across pooled connections.
- ACLs derived from corpus structure by a **declarative rule file**
  ([`corpus/acl_rules.yaml`](corpus/acl_rules.yaml)) — 19 rules, first-match-wins,
  auditable without reading Python.
- Hash-chained `audit_log` table for tamper-evident authorization records.
- Integration tests proving two principals get different row counts from the same query.

## The access model

ABAC, not plain RBAC. A request carries `groups`, `clearance`, `department`, `region`, and
an optional expiry; a document carries `sensitivity`, `allowed_groups`, `min_clearance`,
`need_to_know_tags`, and `jurisdiction`.

| Handbook subtree | Sensitivity | Clearance | Groups |
|---|---|---|---|
| `board-meetings/`, `ceo/`, `leadership/` | restricted | executive | `executives` |
| `total-rewards/compensation/`, `stock-options.md` | restricted | manager | `people-ops`, `comp-committee` |
| `people-policies/{france-sas,inc-usa,india-ltd,…}` | confidential | employee | + region-scoped |
| `security/security-operations/` | restricted | employee | `security` |
| `legal/`, `acquisitions/` | confidential | manager | `legal`, `executives` |
| `values/`, `company/`, `communication/` | public | — | anyone |

Compensation, board material, and per-country employment policy are content that plausibly
*should* be restricted at a real company, which is what makes the denial demos meaningful
rather than contrived.

## Architecture

```
src/gatekeeper/
├─ core/        principals, models, RLS-aware session management
├─ ingest/      ACL derivation, chunking, blobs, pipeline
├─ retrieval/   dense search · hybrid, rerank, planning (Phase 3–4)
├─ llm/         embeddings, generation, provider abstraction
├─ evals/       filtered-ANN benchmark · retrieval harness (Phase 3)
├─ redteam/     independent oracle + adversarial corpus
└─ apps/        api · worker · mcp                      (Phase 4–5)
```

Decision records live in [`docs/adr/`](docs/adr/).

## License

MIT. The handbook corpus is MIT-licensed by GitLab B.V. and is fetched at build time, not
vendored.
