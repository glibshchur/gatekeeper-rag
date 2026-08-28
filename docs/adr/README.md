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
| [0006](0006-filtered-ann-and-index-selectivity.md) | Filtered ANN: the access predicate can cost you the vector index | Accepted |
