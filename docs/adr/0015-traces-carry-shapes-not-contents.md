# 0015 — Traces carry shapes, never contents

**Status:** Accepted · **Date:** 2026-08-31 · **Phase:** 5

## Context

Tracing a retrieval pipeline is standard practice, and the standard way to do it is
actively dangerous here.

The attributes that make a retrieval trace useful are the query text and the documents
that matched. Recording them creates a second copy of the most sensitive traffic in the
system, in a different store, under a different retention policy and — in every deployment
this resembles — a much weaker access policy. An operator with Jaeger access could then
read what the CFO searched for and which restricted documents matched, without touching
Postgres, and row-level security would never see the read.

This is not a hypothetical failure of this design; it is the ordinary outcome of
instrumenting a system like this the ordinary way.

## Decision

**Spans carry shapes. Never contents.**

| recorded | not recorded |
|---|---|
| `query.chars`, `query.terms` | the query text |
| `retrieval.returned`, `retrieval.withheld` | document ids, titles, paths, chunk content |
| `entitlement.fingerprint`, `entitlement.clearance` | principal id, email, handle |
| stage latencies, token counts | the generated answer |

`telemetry.attributes()` is the only sanctioned way to build span attributes and it
**raises** on a deny-listed key rather than dropping it. A silently discarded attribute is
indistinguishable from one that was never added, and whoever added it would keep believing
the trace was richer than it is.

The principal is represented by the **entitlement fingerprint already computed for the
query cache** — a hash of exactly the attributes the policy consults. This is not merely
the safe choice, it is the more useful one: traces group by the thing that actually
determines what a query can return, so "why is this bucket slow" is answerable, and it
names nobody.

Tracing is **off unless `GK_OTEL_ENDPOINT` is set**, and `span()` is then a genuine null
context manager rather than a stub wrapping an SDK object. The default demo needs no
collector.

## Consequences

A test walks the AST of every module under `src/gatekeeper/` and fails if any call to
`span()`, `set_attributes()` or `attributes()` names a forbidden key. Runtime refusal is
not enough on its own: a span on the cache-hit path would raise for the first time in
production. A companion test asserts the pipeline *is* instrumented, so the deny-list test
cannot pass by tracing nothing.

**The first trace immediately found something.** The `search` root span was 42 ms while its
children accounted for 11. The gap was the `count_withheld` baseline query — the unfiltered
comparison that makes "3 results withheld by authorization" possible — at 18 ms against
10 ms for the authorized query it is compared against. The transparency feature costs
nearly twice the retrieval. It now has its own span, because that is worth knowing before
enabling it on a hot path.

**Two entry points had to be instrumented, which surfaced a divergence.**
`retrieval.pipeline.retrieve` (configurable, reranked — what the console uses) and
`retrieval.search.search` (dense-only — what the CLI uses) are separate paths. The CLI's
`ask` has never used the reranker. That is now visible in a trace rather than only in the
source.

The root span is a thin wrapper around a renamed `_retrieve`/`_search` rather than an
indentation of the body. The stages stay at one level and the tracing is a ring around
them; a diff that reindents an entire function to add observability is a diff nobody can
review.
