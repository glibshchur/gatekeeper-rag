# gatekeeper-rag

**A permission-aware enterprise RAG platform where authorization is enforced inside the
database, not in application code.**

Most internal-chatbot demos answer questions well and handle access control by filtering
results in Python after retrieval. That is the wrong layer: a bug, an injected prompt, or
a compromised API process leaks documents. `gatekeeper-rag` pushes authorization into
Postgres row-level security, so the database itself refuses to return rows the caller is
not cleared to see — and the query layer connects as a role that *cannot* bypass it.

> **Status: Phase 0 of 6.** Foundations and the authorization substrate are in place.
> Retrieval lands in Phase 1. See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the full roadmap.

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
real departmental structure, and loads it into Postgres.

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
├─ ingest/      ACL derivation, corpus loaders          (Phase 0–1)
├─ retrieval/   hybrid search, fusion, rerank, planning (Phase 3–4)
├─ llm/         provider abstraction                    (Phase 1)
├─ evals/       harness, metrics, regression gates      (Phase 3)
├─ redteam/     exfiltration + injection attack suite   (Phase 2–4)
└─ apps/        api · worker · mcp                      (Phase 4–5)
```

Decision records live in [`docs/adr/`](docs/adr/).

## License

MIT. The handbook corpus is MIT-licensed by GitLab B.V. and is fetched at build time, not
vendored.
