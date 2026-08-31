# Architecture Decision Records

One file per decision that was expensive to make or would be expensive to reverse.
Format is loosely [MADR](https://adr.github.io/madr/): context, the decision, what it
costs, and what was rejected.

A record is written when the decision is made, not afterwards. Superseded records stay in
place with a pointer forward — the reasoning that turned out to be wrong is usually more
useful than the reasoning that turned out to be right.

| # | Decision | Status |
|---|---|---|
| [0001](0001-postgres-as-the-only-datastore.md) | Postgres is the only datastore | Accepted |
| [0002](0002-row-level-security-over-application-filtering.md) | Authorization lives in row-level security | Accepted |
| [0003](0003-abac-over-rbac.md) | Attribute-based access control, not role-based | Accepted |
| [0004](0004-embedding-columns-on-chunks.md) | Embeddings are columns on `chunks`, one per vector space | Accepted |
| [0005](0005-one-decision-function-and-deny-as-data.md) | One decision function; deny rules as data | Accepted |
| [0006](0006-filtered-ann-and-index-selectivity.md) | Filtered ANN: the access predicate can cost you the vector index | Resolved (Phase 3) |
| [0007](0007-explicit-claims-not-ambient-session-state.md) | Authorization reads its inputs as arguments, not from the session | Accepted |
| [0008](0008-hybrid-search-measured-and-disabled.md) | Hybrid search: built, measured, and off by default | Accepted |
| [0009](0009-mcp-surface-and-one-principal-per-process.md) | MCP: one principal per process; withheld results are counts | Accepted |
| [0010](0010-injection-detection-is-the-second-line.md) | Injection detection is the second line; the semantic layer did not work | Accepted |
| [0011](0011-cache-by-entitlement-not-identity.md) | The query cache is keyed by entitlement and stores decisions, not data | Accepted |
| [0012](0012-groundedness-needs-two-layers.md) | Groundedness needs a numeric check; embeddings cannot see numbers | Accepted |
