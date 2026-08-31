# The users with the least access were paying the most latency

*Filtered ANN under row-level security, and why the obvious fix was on the wrong axis.*

---

I built a RAG system where authorization is enforced by Postgres row-level security rather
than by filtering in Python. The pitch is easy: the database refuses to return rows the
caller isn't cleared to see, so a bug in the application can't leak a document that never
left the database.

Then I measured it, and found the opposite of what I expected. Not a security problem — a
**fairness** problem, invisible from the outside, that punished exactly the people the
system restricts most.

## The measurement

73,801 chunks of the GitLab handbook, HNSW index over 384-dimensional `halfvec`
embeddings, an ABAC policy as an RLS predicate on the same relation. Two principals, same
query:

| principal | can read | plan | p50 |
|---|---:|---|---:|
| `guest` | 5.2% of the corpus | `Parallel Seq Scan on chunks` | **79 ms** |
| `raj` | 84.6% of the corpus | `Index Scan using ...hnsw` | **2.2 ms** |

A 36× gap, triggered by *who was asking* rather than by what they asked. (An earlier
`EXPLAIN ANALYZE` pass on a colder cache put it at 79×; these are the numbers from the
controlled isolation run below, and they are the ones I'd defend.)

Below a selectivity threshold, Postgres estimates the access predicate will leave roughly
one row and concludes that scanning the table beats walking a graph and filtering its
output. That estimate isn't unreasonable. It's just catastrophic in aggregate, and there
is **nothing in the results that indicates it happened**. Recall stays at 1.000 — a
sequential scan is exact. A tenant whose users are tightly scoped runs slowly forever and
never sees an error.

That's the part worth internalising. The failure mode of filtered ANN isn't wrong answers.
It's right answers, arriving slowly, for the users you'd least like to inconvenience.

## The fix I proposed, and why it was wrong

I wrote an ADR proposing **per-tenant partial indexes**: give each tenant its own HNSW
graph so a restrictive principal searches a small index instead of filtering a large one.
Clean, obvious, and completely useless here.

The cliff is **intra-tenant**. `guest` and `mira` are in the same tenant and differ 18× in
what they can read. A per-tenant index would have helped neither of them.

The axis that works is `sensitivity` — four values, present in the policy verbatim, and
correlated with selectivity because the most restricted principals are precisely the ones
limited to `public`. Partial HNSW graphs for the `public` and `public+internal` tiers; the
public graph is **4.4 MB against 81 MB** for the full one.

I only found this because I re-read my own ADR against the data instead of implementing
it. The diagnosis was right; the prescription was pattern-matching.

## Most of the fix was an accident

Here's the result that actually changed how I work. I isolated the improvement by
downgrading the policy function and re-running against the same corpus and the same
queries:

| `authorize()` form | guest plan | guest p50 | raj p50 |
|---|---|---:|---:|
| `STABLE`, called per row | `Parallel Seq Scan` | 79.0 ms | 2.2 ms |
| `IMMUTABLE`, inlinable | `HNSW (full)` | 6.1 ms | 2.2 ms |
| + coarse predicate & partial index | `HNSW (public partial)` | **2.0 ms** | 2.2 ms |

**13× of the 40× came from a change I'd made for a completely unrelated reason** — fixing
an 11× slowdown on lexical queries, weeks earlier in the project's narrative.

An opaque `STABLE` function gives the planner a default selectivity guess. That guess made
the sequential scan look cheap. Making the predicate `IMMUTABLE` and inlinable let Postgres
fold the policy's clauses into the query's quals and estimate them properly.

The cliff and the lexical slowdown were **the same root cause** — a predicate the planner
couldn't see into — and neither investigation noticed that at the time. Two separate
performance problems, two separate diagnoses, one shared cause that only became visible
because the benchmark recorded the chosen *plan* on every row rather than just the latency.

## The remaining 3× is deliberate

The policy is one SQL function, and it stays the only authority. But the query also
restates two of its clauses so the planner can index them:

```sql
min_clearance <= :clearance
AND (sensitivity = 'public' OR allowed_groups && :groups)
```

This is a **superset** of what the policy permits — it can only remove rows the policy
would also remove. That's a dangerous kind of claim to make by reasoning, so it isn't made
by reasoning: a test compares result sets with and without the coarse clauses and asserts
they're identical, rather than arguing about which clauses are safe to restate.

## A benchmark that reports what you expected is not evidence

Two measurement bugs produced confident wrong numbers before any of the above was written,
and either would have shipped as a finding.

**Prepared-statement plan reuse.** The exact and approximate queries were byte-identical
SQL differing only in a planner GUC. Prepared plans aren't invalidated by GUC changes, so
the sequential-scan plan prepared for ground truth was reused for every measurement.
Latency matched to within noise; recall was a perfect 1.000. Both wrong, both entirely
plausible.

**Buffer-pool eviction.** Computing exact ground truth scans the whole table and evicts the
index from `shared_buffers`, so the first timed measurements were cold reads.

Recall of 1.000 at *every* selectivity should have been suspicious on sight. It wasn't,
because it was the number I wanted.

## What I'd tell someone building this

**Measure the plan, not the latency.** `EXPLAIN ANALYZE` on the authorized query, per
principal, as a first-class part of the benchmark. Timings tell you something is slow;
plans tell you why, and they're what let two unrelated investigations turn out to be one.

**Check the cost distribution across principals, not the average.** The average was fine.
The average is always fine. The cliff lives entirely in the tail of *who*, not the tail of
*what*.

**Make the policy inlinable.** `IMMUTABLE` over `STABLE` where the semantics allow, and
read the claims as explicit arguments rather than from session state, so the planner can
fold and estimate them. This is worth more than any index you'll add.

**Restate policy clauses in the query only with a test that pins the superset property.**
The performance is real and so is the risk.

---

*From [gatekeeper-rag](https://github.com/OWNER/gatekeeper-rag). Numbers reproduce with
`make bench`; the full record is [ADR 0006](../adr/0006-filtered-ann-and-index-selectivity.md),
including the parts where it was wrong.*
