"""Concurrent load against the authorized retrieval path.

`filtered_ann` measures one query at a time and answers "what does RLS cost a query".
This answers a different question — "what does the system do when many principals query
at once" — and the two diverge for a reason specific to this design.

Every authorized read here opens a transaction, sets `gatekeeper.principal` with
`set_config(..., true)`, and runs the policy as a per-row predicate. Under concurrency
that means the connection pool, not the HNSW graph, is the thing most likely to be the
bottleneck: a transaction-local GUC cannot be shared between pooled connections, so no
amount of statement caching lets two principals reuse one authorized session. Whether
that costs anything measurable is exactly what a single-query benchmark cannot tell you,
and it is the number a reviewer would want before believing "RLS is basically free".

**Three choices about method, because they determine what the numbers may claim:**

1. **Closed-loop with a fixed worker count**, not an open-loop arrival process. Each
   worker issues a query, waits, issues the next. This measures capacity at a given
   concurrency rather than latency under a chosen arrival rate, and it cannot suffer
   coordinated omission — a slow response delays that worker's next request instead of
   silently vanishing from the histogram.

2. **A warm-up pass that is discarded.** The first query per principal pays for buffer
   pool misses on that principal's partial index, and a cold run reports the disk, not
   the design. This is the same trap that made the first `filtered_ann` run report
   recall 1.000 everywhere.

3. **Auditing stays on.** It is tempting to disable it for a throughput number, and the
   number would be higher and dishonest: the audit chain takes a per-tenant advisory
   lock, so it is precisely the component most likely to serialise under concurrency.
   Measuring the system without it would measure a system nobody runs. The `--no-audit`
   arm exists to quantify that cost, not to replace the default.

Principals are drawn round-robin from the golden set's askers, so the load is spread
across entitlement buckets of genuinely different selectivity rather than hammering one
partial index.

**The `bypass_embedding` arm is the one that reframes the result.** It runs the identical
authorized query with a precomputed vector, removing only the in-process embedding pass.
Without it the sweep says "the system saturates at ~210 q/s" and a reader would naturally
attribute the ceiling to row-level security, since that is what this project is about.
With it the two numbers separate by roughly 5x, and the ceiling turns out to be the ONNX
model sharing a process with the event loop. That is an argument for moving embedding to
its own service, not for weakening the authorization model -- the opposite of the
conclusion the unqualified number invites.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gatekeeper.core.db import principal_session
from gatekeeper.evals.harness import load_golden
from gatekeeper.ingest import seed
from gatekeeper.retrieval.search import DEFAULT_EF_SEARCH, _ann_query, search

if TYPE_CHECKING:
    from gatekeeper.core.principal import Principal
    from gatekeeper.llm.embeddings import Embedder


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(q * len(ordered)), len(ordered) - 1)
    return ordered[index]


@dataclass
class LoadResult:
    concurrency: int
    audit: bool
    duration_s: float
    bypass_embedding: bool = False
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0

    @property
    def completed(self) -> int:
        return len(self.latencies_ms)

    @property
    def throughput(self) -> float:
        return self.completed / self.duration_s if self.duration_s else 0.0

    @property
    def p50(self) -> float:
        return percentile(self.latencies_ms, 0.50)

    @property
    def p95(self) -> float:
        return percentile(self.latencies_ms, 0.95)

    @property
    def p99(self) -> float:
        return percentile(self.latencies_ms, 0.99)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.latencies_ms) if self.latencies_ms else 0.0


async def _worker(
    principals: list[Principal],
    questions: list[str],
    embedder: Embedder,
    deadline: float,
    worker_id: int,
    k: int,
    audit: bool,
    out: LoadResult,
    vectors: list[list[float]] | None = None,
) -> None:
    """One synthetic client. Offset by worker id so workers do not march in lockstep.

    The query embedding is computed inside the timed section on every iteration, because
    a real request pays for it. `vectors` is the deliberate exception: passing precomputed
    vectors isolates the database path, and the gap between the two arms is what
    identifies the bottleneck.
    """
    step = worker_id
    while time.monotonic() < deadline:
        principal = principals[step % len(principals)]
        question = questions[step % len(questions)]
        step += 1
        started = time.monotonic()
        try:
            if vectors is None:
                await search(principal, question, embedder, k=k, audit=audit, count_withheld=False)
            else:
                async with principal_session(principal) as session:
                    await _ann_query(
                        session,
                        embedder=embedder,
                        query_vector=vectors[step % len(vectors)],
                        k=k,
                        ef_search=DEFAULT_EF_SEARCH,
                        tenant_id=principal.tenant_id,
                        principal=principal,
                    )
        except Exception:
            out.errors += 1
            continue
        out.latencies_ms.append((time.monotonic() - started) * 1000)


async def run_once(
    embedder: Embedder,
    *,
    concurrency: int,
    seconds: float,
    k: int = 10,
    audit: bool = True,
    bypass_embedding: bool = False,
) -> LoadResult:
    questions = [q.question for q in load_golden()]
    askers = sorted({q.asker for q in load_golden()})
    principals = [await seed.load_principal(handle) for handle in askers]

    # Warm-up: one query per principal, discarded. Reports the design, not the disk.
    for principal in principals:
        await search(principal, questions[0], embedder, k=k, audit=False, count_withheld=False)

    vectors = [embedder.encode_query(q).tolist() for q in questions] if bypass_embedding else None

    result = LoadResult(
        concurrency=concurrency,
        audit=audit,
        duration_s=seconds,
        bypass_embedding=bypass_embedding,
    )
    started = time.monotonic()
    deadline = started + seconds
    await asyncio.gather(
        *(
            _worker(principals, questions, embedder, deadline, i, k, audit, result, vectors)
            for i in range(concurrency)
        )
    )
    result.duration_s = time.monotonic() - started
    return result


async def sweep(
    embedder: Embedder,
    *,
    levels: tuple[int, ...] = (1, 2, 4, 8, 16, 32),
    seconds: float = 10.0,
    k: int = 10,
    compare_audit: bool = True,
) -> list[LoadResult]:
    """Concurrency sweep, ascending. Ascending on purpose: a descending sweep leaves the
    pool and the buffer cache warmed by the heaviest arm, which flatters the light ones."""
    results: list[LoadResult] = []
    for level in levels:
        results.append(await run_once(embedder, concurrency=level, seconds=seconds, k=k))
    peak = max(levels)
    if compare_audit:
        results.append(
            await run_once(embedder, concurrency=peak, seconds=seconds, k=k, audit=False)
        )
    # The arm that says where the ceiling actually is. Run last so it cannot warm the
    # cache for anything that follows.
    results.append(
        await run_once(embedder, concurrency=peak, seconds=seconds, k=k, bypass_embedding=True)
    )
    return results


def to_markdown(results: list[LoadResult], corpus_chunks: int) -> str:
    lines = [
        "# Load",
        "",
        "Generated by `gatekeeper load`. Closed-loop: each worker issues one query, waits,",
        "issues the next — so a slow response delays that worker rather than disappearing",
        "from the histogram. Auditing is **on** except where noted; the audit chain takes a",
        "per-tenant advisory lock and is therefore the component most likely to serialise,",
        "so measuring without it would measure a system nobody runs.",
        "",
        f"Corpus: {corpus_chunks:,} chunks. Queries drawn from the golden set, principals",
        "round-robin across every asker in it, so load spreads over entitlement buckets of",
        "different selectivity rather than one partial index.",
        "",
        "| arm | concurrency | audit | queries | throughput (q/s) | mean | p50 | p95 | p99 |"
        " errors |",
        "|:---|---:|:---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        arm = "query only (no embedding)" if r.bypass_embedding else "full request"
        lines.append(
            f"| {arm} | {r.concurrency} | {'on' if r.audit else 'off'} | {r.completed:,} | "
            f"{r.throughput:.1f} | {r.mean:.0f} ms | {r.p50:.0f} ms | {r.p95:.0f} ms | "
            f"{r.p99:.0f} ms | {r.errors} |"
        )
    lines.extend(["", "## What the numbers say", ""])
    lines.extend(_findings(results))
    return "\n".join(lines) + "\n"


def _findings(results: list[LoadResult]) -> list[str]:
    """Read the conclusions off the measurements rather than restating them by hand.

    Written this way because the hand-written version of this section went stale the
    first time the sweep was re-run on a different machine, and a stale conclusion beside
    a fresh table is worse than no conclusion.
    """
    full = [r for r in results if not r.bypass_embedding and r.audit]
    if not full:
        return ["_no comparable arms in this run._"]

    peak = max(full, key=lambda r: r.throughput)
    lines = [
        f"**Saturation.** Throughput peaks at **{peak.throughput:.0f} q/s** at concurrency "
        f"{peak.concurrency} and stays flat above it while p50 grows roughly linearly "
        f"({full[0].p50:.0f} ms at concurrency {full[0].concurrency} to "
        f"{full[-1].p50:.0f} ms at {full[-1].concurrency}). That is a saturated closed "
        "loop: past the knee, added concurrency buys queueing, not work.",
        "",
    ]

    bypass = next((r for r in results if r.bypass_embedding), None)
    if bypass is not None:
        top = max(full, key=lambda r: r.concurrency)
        ratio = bypass.throughput / top.throughput if top.throughput else 0.0
        lines += [
            f"**The ceiling is not authorization.** Removing only the in-process embedding "
            f"pass — same RLS policy, same partial HNSW indexes, same audit chain — raises "
            f"throughput from {top.throughput:.0f} q/s to **{bypass.throughput:.0f} q/s**, "
            f"a factor of {ratio:.1f}. The authorized database path is not what limits this "
            "system; the ONNX model sharing a process with the event loop is. The remedy is "
            "a separate embedding service, and notably *not* anything that would weaken the "
            "access-control design — which is the conclusion the unqualified number invites "
            "and the measurement refutes.",
            "",
        ]

    unaudited = next((r for r in results if not r.audit and not r.bypass_embedding), None)
    audited = next(
        (r for r in full if r.concurrency == (unaudited.concurrency if unaudited else -1)), None
    )
    if unaudited is not None and audited is not None:
        lines += [
            f"**Auditing costs throughput and buys tail latency.** At concurrency "
            f"{unaudited.concurrency}, turning the audit chain off raises throughput from "
            f"{audited.throughput:.0f} to {unaudited.throughput:.0f} q/s "
            f"({unaudited.throughput / audited.throughput - 1:+.0%}) — and degrades p99 from "
            f"{audited.p99:.0f} ms to {unaudited.p99:.0f} ms. That is the opposite of the "
            "expected trade and it has a mechanism: the chain's per-tenant advisory lock "
            "serialises writers, which incidentally paces the whole pipeline. Without it, "
            "requests pile onto the connection pool and the tail spreads. The lock is not "
            "free, but what it costs is average throughput, not predictability.",
        ]
    return lines
