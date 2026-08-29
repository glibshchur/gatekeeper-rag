# gatekeeper-rag

**A permission-aware enterprise RAG platform where authorization is enforced inside the
database, not in application code.**

Most internal-chatbot demos answer questions well and handle access control by filtering
results in Python after retrieval. That is the wrong layer: a bug, an injected prompt, or
a compromised API process leaks documents. `gatekeeper-rag` pushes authorization into
Postgres row-level security, so the database itself refuses to return rows the caller is
not cleared to see — and the query layer connects as a role that *cannot* bypass it.

> **Status: Phase 4 of 6.** Retrieval quality is now measured: a hand-written golden set,
> a stage-by-stage ablation, hybrid search, and cross-encoder reranking. Contextual
> retrieval and agentic multi-hop land in Phase 4. See [PROJECT_PLAN.md](PROJECT_PLAN.md)
> for the full roadmap.

## Results

| | |
|---|---|
| Leaks across 360 adversarial probes + 6 direct-fetch + 8 boundary probes | **0** |
| (principal, chunk) pairs where the database and an independent oracle disagree | **0 of 442,782** |
| Over-block rate (entitled results withheld) | **1.15%** |
| nDCG@10 on 58 hand-written questions (`dense+rerank`) | **0.796** |
| Retrieval latency p50, dense / dense+rerank | **8 ms / 215 ms** |
| Recall@10 vs exact brute force, `ef_search=200` | **1.000** |

The authorization numbers come from [`src/gatekeeper/redteam/`](src/gatekeeper/redteam/),
which scores the SQL policy against a **separate Python implementation of the same written
spec**. Asking the database whether the database got it right proves nothing; two
implementations disagreeing loudly is the point. Retrieval numbers are in
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md), measured against exact brute-force ground truth.

Retrieval quality is in [`docs/ABLATION.md`](docs/ABLATION.md), measured over 58
hand-written questions labelled against real documents — never generated from chunk text,
which would make the eval circular and hand lexical search a win by construction.

**Three of the most useful results in this project are negative**, and they are reported
as prominently as the positive ones:

- The selectivity cliff — 79x worse latency for the most restricted users, silently — is
  **fixed**, 79 ms → 2.0 ms. But 13x of the 40x came from a change made for an unrelated
  reason, and this ADR's own proposed fix was on the wrong axis
  ([ADR 0006](docs/adr/0006-filtered-ann-and-index-selectivity.md)).
- **Hybrid search does not pay on this corpus** and is off by default: 0.797 against
  `dense+rerank`'s 0.796, for 56% more latency
  ([ADR 0008](docs/adr/0008-hybrid-search-measured-and-disabled.md)).
- A performance fix to the ABAC predicate caused a *correctness* regression — retrieval
  returned zero results for a principal who could plainly `SELECT` two matching rows
  ([ADR 0007](docs/adr/0007-explicit-claims-not-ambient-session-state.md)).

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

## Use it from Claude Code

The repo ships a [`.mcp.json`](.mcp.json), so an MCP client in this directory picks the
server up directly:

```bash
make mcp WHO=raj      # or edit GK_MCP_PRINCIPAL in .mcp.json
```

Three tools — `search_knowledge_base`, `get_document`, `whoami` — all going through the
same row-level security policy as the CLI and the console, with no agent-specific code
path. Ask the same question as two principals and the difference is visible in the answers:

```text
raj   → Searched as Raj Mehta — Backend Engineer (clearance 1).
        3 additional match(es) exist that this principal is not authorised to read.
        1. handbook/eba/_index.md                    internal
        2. handbook/communication/ask-me-anything.md public

mira  → Searched as Mira Lindqvist — CFO (clearance 3).
        1. handbook/people-group/…/leave-types.md    restricted
        2. handbook/leadership/compensation-review-conversations.md  restricted
```

Two rules the tool output follows, both about what a model will do with what you hand it
([ADR 0009](docs/adr/0009-mcp-surface-and-one-principal-per-process.md)):

- **A withheld result is a count, never an identity.** Hand a model the *title* of a
  withheld document and it will write that title into its answer — and for restricted
  material the title is usually the secret.
- **`get_document` returns byte-identical responses for an unreadable path and a
  nonexistent one.** Distinguishing them turns the tool into an oracle for enumerating the
  corpus by probing paths.

The server binds to **one principal for its lifetime**, set at launch. Unlike the console
below, no tool takes a principal argument — so no prompt injection can ask for a different
one.

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

## What Phase 4 delivers so far

- **MCP server** over stdio (`make mcp`), exposing the corpus as three authorization-scoped
  tools with no separate query path — so the red-team suite's guarantees cover it without
  re-testing.
- One principal per process, bound at launch; the server refuses to start on an expired
  grant rather than starting and failing every call.
- Withheld results reported as counts; unreadable and nonexistent paths indistinguishable.

## What Phase 3 delivers

- **58-question golden set**, hand-written against verified documents, each labelled with
  the principal entitled to the answer — so metrics describe the system as deployed rather
  than an unauthorized ideal. A validation pass fails the run if any label is unreachable.
- **Stage-by-stage ablation** where the arms differ only in a `RetrievalConfig`, never in
  code path. Recall@k, MRR, nDCG@10, per-category breakdown, and the questions the best
  configuration still misses.
- **Lexical retrieval** (`tsvector`, OR-of-lexemes, `ts_rank_cd`) and **Reciprocal Rank
  Fusion**, both under the same RLS policy as the dense path.
- **Cross-encoder reranking** (`ms-marco-MiniLM-L-6-v2` via ONNX, CPU, no API key):
  +6% MRR, +4% nDCG. The only unambiguous win of the phase.
- **8x faster authorization** by passing claims as an argument instead of reading them
  ambiently in the predicate — 797 ms → 100 ms on a lexical query.

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
├─ retrieval/   dense · lexical · RRF · config-driven pipeline
├─ llm/         embeddings, generation, provider abstraction
├─ evals/       golden set, ablation, filtered-ANN benchmark
├─ redteam/     independent oracle + adversarial corpus
└─ apps/        api · worker · mcp                      (Phase 4–5)
```

Decision records live in [`docs/adr/`](docs/adr/).

## License

MIT. The handbook corpus is MIT-licensed by GitLab B.V. and is fetched at build time, not
vendored.
