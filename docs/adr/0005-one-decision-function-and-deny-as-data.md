# 0005 — One decision function, and deny rules as data

**Status:** Accepted · **Date:** 2026-08-28 · **Phase:** 2

## Context

Phase 0 wrote the access predicate inline in two RLS policies, one on `documents` and one
on `chunks`. That was fine for three rules. Phase 2 adds need-to-know, jurisdiction,
expiry, and deny rules, and two consequences appear immediately:

1. **Duplicated predicates drift.** Two copies of a seven-clause rule, edited by hand in
   migrations, will eventually disagree. In this system a disagreement between the
   document policy and the chunk policy is a leak or an outage, depending on direction.
2. **Some rules are not deployment-time facts.** A litigation hold, a contractor
   restriction, a temporary embargo — these change on a business timescale, not a release
   timescale. Encoding them in migrations means a lawyer's request becomes a deploy.

## Decision

**One function.** `gatekeeper.authorize(tenant, sensitivity, groups, min_clearance, tags,
jurisdiction)` implements the whole model. Both policies call it and nothing else. The
access model is written down once, in one place, in the order the rules are evaluated.

**Deny rules as rows.** The `policies` table holds deny rules as JSON predicates.
`gatekeeper.denied_tags()` collapses every rule applicable to the current principal into a
single tag set, and `authorize()` checks `NOT (tags && denied_tags())`.

**Deny is evaluated before any grant.** Not because the boolean algebra requires it — it
does not — but because the refusal reason should name the rule that actually decided, and
that makes disagreements between the SQL and the oracle debuggable.

## Consequences

**What this buys.** The predicate exists once, so `documents` and `chunks` cannot drift.
Adding an attribute is one function body, not N policies. A hold can be added or lifted
with an `INSERT` or an `UPDATE ... SET enabled = false`, auditable as data, with no
migration. And because the model is a single readable function, transcribing it into an
independent oracle for cross-checking is realistic — which is what makes the
whole-corpus reconciliation in the red-team suite possible.

**What this costs.** A function call per row, on every scan. That is the reason
`denied_tags()` returns a *set* rather than being evaluated per row: a correlated subquery
against `policies` inside an RLS predicate runs once per candidate row, which on a
74,000-chunk vector scan is not survivable. The collapse to one array comparison is the
only thing that makes data-driven deny affordable here.

The predicate is also opaque to the planner, and that turns out to matter far more than
the function-call overhead — see [ADR 0006](0006-filtered-ann-and-index-selectivity.md).

**What is still missing.** Only deny rules are data. Allow rules remain in the function
body. A full policy language would put both in the table, at the cost of a compiler and a
much larger surface to verify. That trade is not obviously worth making, and Phase 2 does
not make it.

## Alternatives considered

- **Compile all policies into `CREATE POLICY` statements at deploy time.** Best planner
  behaviour, because the predicate becomes literal SQL. Rejected because it puts a policy
  change back on the release path, which was one of the two problems being solved.
- **Cedar or OPA as the decision point.** More expressive, well-specified, someone else's
  bugs. Rejected for the same reason as in ADR 0002: the decision must be made *by* the
  database, not handed to it. Compiling Cedar *into* the SQL predicate remains open.
- **A single denormalised `visible_to text[]` column, maintained on write.** Turns every
  read into an array overlap the index can use — genuinely attractive given ADR 0006.
  Rejected for now because it makes the access model a materialisation problem: a policy
  change would require rewriting every affected row, and a missed rewrite is a silent
  leak that no reconciliation would catch until it ran.
