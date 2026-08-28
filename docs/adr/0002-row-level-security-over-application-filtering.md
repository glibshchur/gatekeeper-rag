# 0002 — Authorization lives in row-level security, not application code

**Status:** Accepted · **Date:** 2026-08-28 · **Phase:** 0

## Context

The common pattern is a `WHERE` clause built in application code:

```python
rows = await db.fetch(SELECT_CHUNKS_SQL, allowed_groups=user.groups)
```

This works until it does not. It fails when a new endpoint forgets the clause, when a
debugging query is added, when an ORM relationship loads lazily through a path nobody
checked, when a background job runs without a user context, or when an injected prompt
persuades an agent to call a tool that queries by primary key. Every one of those is a
plausible bug, and each one leaks documents.

The deeper problem is that the guarantee is unfalsifiable. "We filter by group" is a
statement about every code path that will ever exist, and it cannot be tested — only the
paths someone thought to write a test for are covered.

## Decision

Authorization is enforced by Postgres row-level security. Three things make it real:

1. **Claims travel in a transaction-local GUC.** `principal_session()` opens a
   transaction and calls `set_config('gatekeeper.principal', <claims json>, true)` before
   any query runs. RLS policies read it through helper functions in the `gatekeeper`
   schema. `is_local => true` scopes the setting to the transaction, so authorization
   context cannot survive into the next checkout of a pooled connection.

2. **Policies are `ENABLE`d *and* `FORCE`d.** `ENABLE ROW LEVEL SECURITY` exempts the
   table owner. Without `FORCE ROW LEVEL SECURITY`, the application — which owns the
   tables in most deployments — bypasses every policy, and the guarantee is theatre.

3. **The query plane connects as a role that cannot bypass RLS.** `gatekeeper_app` is
   `NOSUPERUSER NOBYPASSRLS` with DML grants only. Migrations and ingestion use a separate
   owner connection. A compromised API process holds a connection that is *incapable* of
   reading unauthorized rows.

## Consequences

**What this buys.** The guarantee becomes falsifiable, and therefore testable: point any
query at the database as the app role and the unauthorized rows are not there. Aggregates
cannot leak existence. A forgotten `WHERE` clause is no longer a vulnerability. Fetching
a known primary key returns nothing. `tests/integration/test_rls.py` asserts each of
these against rows the database actually returned.

It also **fails closed**. An unset GUC yields `NULL`, and `tenant_id = NULL` is false, so
a session that forgets to set claims sees zero rows rather than the entire table. That
default is the single most important property of the design.

**What this costs.** Policy predicates run on every row the planner touches, which
interacts badly with ANN indexes (see ADR 0001). Debugging is harder — "why is this row
missing" now has an answer inside the database. Migrations must be careful: adding a
table without a policy silently makes it invisible rather than public, which is the right
failure direction but still surprising. And two DSNs must be kept straight; `admin_session()`
exists precisely so that every bypass is explicit and greppable.

## Alternatives considered

- **A repository layer that centralises the filter.** Cheaper and easier to debug.
  Rejected because it is still application code: the guarantee holds only as long as
  nobody writes a query outside the layer, which is not a property you can test for.
- **A policy engine (OPA/Cedar) in front of the database.** Better policy expressiveness,
  and worth revisiting in Phase 2 for *authoring*. Rejected as the enforcement point,
  because it still produces a filter that the database is free to ignore.
- **Separate schemas or databases per tenant.** Strong isolation between tenants, no help
  at all within one — and the interesting failures in this system are intra-tenant.
