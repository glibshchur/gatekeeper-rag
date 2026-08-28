# 0003 — Attribute-based access control, not role-based

**Status:** Accepted · **Date:** 2026-08-28 · **Phase:** 0

## Context

The predecessor to this project used a single mechanism: each document carried
`allowed_groups`, each user carried `groups`, and retrieval used Postgres array overlap
(`&&`). It is a clean model and it covers a lot of ground.

It stops covering ground at the first real requirement that is not about group
membership. Three showed up immediately in the handbook corpus:

- **Seniority.** Board minutes should be gated by level, not by adding everyone senior to
  a `board` group and maintaining it by hand.
- **Jurisdiction.** `people-policies/india-ltd/` and `people-policies/france-sas/` are
  employment policy for different legal entities. A France-based employee retrieving the
  India policy as authoritative is a compliance problem, not a permissions one — and it
  is not expressible as group membership without a group per country per policy area.
- **Time.** A contractor's access should expire on a date, not when someone remembers to
  remove them from a group.

Modelling each of these as more groups produces group explosion: the combinatorial
product of department × level × region × duration, maintained by hand, with no way to ask
"why can this person see this document?"

## Decision

Principals and resources both carry attributes, and access is a predicate over both.

| Principal | Resource |
|---|---|
| `groups[]` | `allowed_groups[]` |
| `clearance` (0 external → 3 executive) | `min_clearance` |
| `region` | `jurisdiction[]` |
| `department` | `owner_group` |
| `valid_until` | `sensitivity`, `need_to_know_tags[]` |

Rules are **data**, not code, in two places: `corpus/acl_rules.yaml` maps corpus structure
to resource attributes at ingest, and the `policies` table holds the runtime rules the
Phase 2 engine compiles into SQL predicates.

## Consequences

**What this buys.** Clearance is ordered, so seniority is a comparison rather than a
group. Region is available for jurisdiction scoping without a group per country. Expiry
is an attribute on the principal, enforced in `Principal.to_claims()`, which refuses to
serialise an expired grant rather than quietly issuing one. And because the rules are
data, "who can see what" is answerable by reading a YAML file — `corpus/acl_rules.yaml`
is 19 rules and auditable by someone who does not read Python.

Crucially, clearance is a **ceiling, not a key**. A high clearance without the right group
grants nothing; `test_clearance_alone_does_not_grant_access` pins that behaviour. This is
the property that separates the model from a simple classification hierarchy, and it is
why the CFO in the demo cast still cannot read security-operations runbooks.

**What this costs.** More attributes mean more ways to misconfigure. The predicate is
longer and harder for the planner to optimise than a single array overlap. And the model
is only half-built at Phase 0: `region`, `need_to_know_tags`, and `valid_until` are
stored, indexed, and populated, but only `groups`, `clearance`, and `sensitivity` are
enforced in SQL. That gap is deliberate — the Phase 0 predicate is intentionally close to
the naive model it replaces so the Phase 2 engine can be measured against a real baseline
— but until Phase 2 lands, the schema promises more than the policies deliver.

## Alternatives considered

- **Keep pure RBAC and add groups as needed.** Simplest thing that could work. Rejected on
  group explosion, and because "why can this person see this" becomes unanswerable.
- **Full policy language (Cedar, OPA/Rego) from the start.** More expressive than what is
  built here. Deferred rather than rejected: adopting one in Phase 0 would mean the
  enforcement point moves out of the database, contradicting ADR 0002. Phase 2 will
  evaluate compiling Cedar policies *into* SQL predicates, which keeps both properties.
- **Label-based mandatory access control (Bell–LaPadula style).** Clean formal model.
  Rejected because real enterprise access is not a total order — security-operations and
  compensation are both restricted, and neither dominates the other.
