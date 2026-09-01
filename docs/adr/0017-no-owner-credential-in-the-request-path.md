# 0017 — No RLS-bypassing credential in the request path

**Status:** Accepted · **Date:** 2026-09-01 · **Phase:** 6 (post-v1.0)

## Context

This project's claim is that authorization is enforced by the database, so a bug in the
application cannot return a row the policy forbids. The mechanism is a split-privilege
connection model: the query layer connects as `gatekeeper_app`, which is `NOSUPERUSER`
and `NOBYPASSRLS`, and policies are `FORCE`d.

That was true of the *connection* and false of the *process*. Writing
[`docs/THREAT_MODEL.md`](../THREAT_MODEL.md) turned up that `admin_session()` — documented
"ingestion and migrations only" — had four callers on the request path. The API process
held `GK_DATABASE_OWNER_URL` in its environment and used it on every request.

RLS bounds a logic bug in the query layer. It does nothing about code execution in a
process holding an owner connection string, which is the difference between "one
principal's entitlements" and "the entire corpus".

## Decision

Move each privileged operation into a `SECURITY DEFINER` function with a narrow return
type, or grant the app role exactly what it needs — and take the owner URL out of the API
entirely.

| Caller | Why it needed privilege | Resolution |
|---|---|---|
| `load_principal` | Reads the caller's entitlements *before* claims exist, so no policy can match | `gatekeeper.resolve_principal(tenant_slug, external_id)` — exact match, at most one row |
| `count_withheld` | The baseline must be genuinely unfiltered, or it under-reports every denial | `gatekeeper.withheld_summary(...)` — returns a count and denied document ids, never content |
| Query cache | 0010 revoked all app-role access | App role granted SELECT/INSERT/UPDATE; DELETE stays owner-only |
| `/api/jobs` | Job rows are not tenant-scoped | App role granted SELECT; writes stay in the worker |

**The principle: a privilege belongs in the database, not in an environment variable.** A
credential can be read by anything running in the process. A `SECURITY DEFINER` function
has a fixed signature and a fixed return type, so the most an attacker gets is the thing
the function was going to return anyway.

**The functions have a minimal owner.** A definer function runs as its owner, and
migrations create objects owned by the superuser that runs them — so the first pass gave
narrow function bodies superuser authority, which is half a fix. `gatekeeper_definer` is
`NOLOGIN`, not a superuser, `BYPASSRLS` (required: `principals` and `chunks` are `FORCE`d),
with `SELECT` on three tables and no write privilege anywhere.

## Consequences

**The test is the point.** `tests/integration/test_owner_credential_boundary.py` points
`database_owner_url` at a host that does not exist and exercises the request path. Anything
that reaches for it fails loudly. `test_the_fixture_actually_bites` asserts that
`admin_session` *does* break under that fixture, because a guard that cannot fail proves
nothing.

That mattered immediately. The first version of the file constructed principals directly
and never called `load_principal`, so it passed while the live API still returned 500 with
the owner URL removed. **Reading the code had missed that caller twice; running it with
the credential taken away found it in one request.** The lesson generalises: to verify that
something is not used, remove it rather than grep for it.

**A deliberate widening, recorded.** Granting the app role SELECT on `query_cache` means a
SQL-injection bug in the request path could read `query_cache.query_text` — what other
entitlement buckets searched for. That is real, and strictly smaller than what the same
attacker previously got from a process holding an owner credential.

**Ordering in the withheld query is load-bearing.** The count is "top-k over everything,
minus what you saw", not "top-k over what you did not see". Filtering before the `LIMIT`
would make a fully-visible result set report *k* withheld instead of 0 — a transparency
feature lying in the alarming direction. The SQL keeps the `LIMIT` on the unfiltered
ranking in a CTE, and `test_the_executive_is_denied_nothing` pins it.

**Parity was verified before the swap was trusted.** The new function was compared against
the old owner-side Python across 5 principals × 8 queries on the live 73,801-chunk corpus —
identical counts and identical denied-document sets in all 48 cases. The red-team suite
still reports 0 leaks and 0 disagreements over 442,806 (principal, chunk) pairs.

**What this does not fix.** Code execution in the API process still lets an attacker query
as whichever principal is being served, and call the definer functions for that tenant.
RLS bounds them to one principal's entitlements at a time rather than to nothing. Process
isolation, not row-level security, is the control for that — and it remains out of scope.
