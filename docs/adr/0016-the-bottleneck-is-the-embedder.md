# 0016 — Under load the bottleneck is the embedder, not the authorization

**Status:** Accepted · **Date:** 2026-08-31 · **Phase:** 5

## Context

[ADR 0006](0006-filtered-ann-and-index-selectivity.md) measured what row-level security
costs a *single* query and closed the selectivity cliff. It could not answer the question a
reviewer actually asks about this design, which is what happens when many principals query
at once.

There is a specific reason to expect concurrency to behave differently here.
`gatekeeper.principal` is a **transaction-local** GUC, set with `set_config(..., true)`.
That is what makes the policy safe under connection pooling — a leaked session variable
cannot outlive its transaction — but it also means no two principals can share an
authorized session, so nothing about the authorized path amortises across callers the way
a plain query would. Whether that costs anything measurable is exactly what a
one-query-at-a-time benchmark cannot say.

## Decision

A closed-loop concurrency sweep (`gatekeeper load`, `docs/LOAD.md`) with three arms: the
full request path, the same path with auditing off, and the same path with the query
vector precomputed.

Closed-loop with a fixed worker count, not an open-loop arrival rate: a slow response then
delays that worker's next request instead of vanishing from the histogram, so the numbers
cannot suffer coordinated omission. Auditing stays on by default, because the audit chain
takes a per-tenant advisory lock and is therefore the component most likely to serialise;
measuring without it would measure a system nobody runs.

## Consequences

**The authorized database path is not the constraint.** Removing only the in-process
embedding pass — identical RLS policy, identical partial HNSW indexes, identical audit
chain — raises throughput **six-fold**, from 201 q/s to 1,207 q/s at concurrency 64
over 73,801 chunks. The ceiling is the ONNX model sharing a process with the event
loop.

This is the single most important number in the project, and specifically because of what
it rules out. Published on its own, "the system saturates at ~200 q/s" invites the reader
to blame row-level security, since that is what this project is about. The bypass arm is
in the sweep permanently so that the comparison is never available without its control.
The remedy it points to is a separate embedding service — not any weakening of the access
model.

**Auditing costs throughput and buys tail latency.** Turning the audit chain off at
concurrency 64 raised throughput 45% (201 to 291 q/s) *and made p99 nearly three times
worse* (361 ms to 992 ms). That is the opposite of the expected trade, and it has a
mechanism: the advisory lock serialises writers and thereby paces the pipeline. Without it,
requests pile onto the connection pool and the tail spreads. The lock is not free, but what
it costs is average throughput, not predictability — which is the better thing to keep.

**The findings section of `docs/LOAD.md` is computed from the results, not written by
hand.** The hand-written version went stale the first time the sweep was re-run, and a
stale conclusion next to a fresh table is worse than no conclusion.

Numbers are from one laptop against one Postgres container and are useful as ratios
between arms, not as absolute capacity.
