# 0007 — Authorization reads its inputs as arguments, not from the session

**Status:** Accepted · **Date:** 2026-08-29 · **Phase:** 3

## Context

Through Phase 2, `gatekeeper.authorize()` read session state itself: it called
`current_tenant()`, `current_groups()`, `denied_tags()` and four others from inside the
predicate. That reads well — the function needs the claims, so it fetches them — and it is
what ADR 0005 shipped, with the claim that collapsing deny rules into one tag set made the
policy lookup "once per statement".

Phase 3's lexical retrieval disproved that. A lexical query matching 8,239 rows:

| | |
|---|---|
| as the table owner, no RLS | 70 ms |
| as a principal, RLS active | **797 ms** |

`STABLE` promises the planner that a function's result will not change *within* a
statement. It does not promise the function will be *called* once. Every candidate row
re-parsed the claims JSON six times and re-queried the `policies` table once.

The dense path had hidden this. HNSW hands the predicate a few hundred candidate rows;
lexical search hands it every row matching the tsquery. Same predicate, two orders of
magnitude difference in how often it runs.

## The attempt that made it worse

Migration 0006 wrapped each accessor in a scalar subquery *inside* `authorize()`, on the
reasoning that an uncorrelated subquery becomes an InitPlan evaluated once. Result:
**2,612 ms**, three times worse than the problem.

Postgres inlines a `LANGUAGE sql` function into the calling query only when its body is a
simple expression. Adding subqueries made the body non-inlinable, so `authorize()` stopped
being an expression the planner could fold into the query's quals and became an opaque
function called once per row — each call now running seven InitPlans of its own. The
optimisation destroyed the mechanism it depended on.

## Decision

Hoist at the call site instead. `gatekeeper.claims_row()` returns every session-derived
value as one composite type, and the policies pass `(SELECT gatekeeper.claims_row())` as
an argument:

```sql
CREATE POLICY chunks_read ON chunks FOR SELECT USING (
    gatekeeper.authorize(
        tenant_id, sensitivity, allowed_groups, min_clearance,
        need_to_know_tags, jurisdiction,
        (SELECT gatekeeper.claims_row())
    )
);
```

The subquery now lives in the outer query, where it is genuinely uncorrelated and becomes
a real InitPlan. `authorize()` returns to being a simple expression over its arguments and
is marked **`IMMUTABLE`** — with the claims passed in, its result depends on nothing else.

**797 ms → 100 ms**, against a 45 ms no-RLS floor.

## Consequences

**The general principle is worth more than the milliseconds.** Moving session state from
*ambient* (read inside the predicate) to *explicit* (passed as an argument) is what made
the problem tractable, and it is the same move that makes a function testable: `authorize()`
is now pure, so its behaviour is a property of its inputs rather than of a session.

**What it costs.** A composite type and a function that must be kept in step with it —
adding an attribute to the access model now means editing `claims_t`, `claims_row()` and
`authorize()` together. The policies are more verbose. And the fix depends on planner
behaviour (InitPlan hoisting, function inlining) rather than on documented API, so a
Postgres upgrade could in principle change it; the lexical benchmark is what would catch
that.

**A caveat on the numbers.** 100 ms is still 2.2x the no-RLS floor. The remaining cost is
`authorize()` evaluated per candidate row — cheap now, but not free, and unavoidable while
the predicate is a function over columns rather than something an index can satisfy. That
is the same limitation as [ADR 0006](0006-filtered-ann-and-index-selectivity.md).

## A correctness regression this caused

Making the predicate cheap changed the planner's mind about which plan to use, and for
principals with very small visible corpora it started choosing the HNSW index where it
had previously sequentially scanned. At extreme selectivity — two visible chunks out of
73,797 — the graph walk never reaches those rows, and **retrieval returned zero results
for a principal who could plainly `SELECT` both**. No error, no short-return signal.

The expensive predicate had been masking the bug by forcing an exact scan. The fix is an
explicit `tenant_id = $1` clause on both retrievers: redundant for security, since RLS
already restricts the tenant, and load-bearing for recall, because it is btree-indexable
and the ABAC predicate is not. It gives the planner information the policy function hides
from it. `test_a_tiny_tenant_still_gets_its_visible_chunks` pins it.
