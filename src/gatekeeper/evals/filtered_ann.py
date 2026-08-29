"""Measure what row-level security costs approximate nearest-neighbour search.

The problem, stated precisely. HNSW is a graph index: a search walks the graph collecting
roughly `ef_search` candidates and returns the best k. Row-level security is applied as a
filter *on the rows the scan produces*. When a principal can see 5% of the corpus, ~95% of
the candidates the graph offers are discarded, and the k that comes back is drawn from a
much smaller and differently-shaped pool than the one the index was built to serve.

Two things can go wrong, and they are not the same thing:

* **Short returns** — fewer than k rows come back although more authorized matches exist.
* **Recall loss** — k rows come back, but they are not the k nearest authorized chunks.

The first is obvious in the output. The second is invisible: the results look fine, they
are simply worse, and no error is raised. Phase 1 turned on `hnsw.iterative_scan` and
observed no short returns, which said nothing at all about the second failure. This
measures it, against exact brute-force ground truth computed under the same RLS policy.

Ground truth is obtained by disabling index scans so the planner must sequentially scan
and compute every distance. That is exact by construction, and slow by construction --
which is the whole reason the index exists.

Two measurement traps, both of which produced confident wrong numbers before being
found. Recording them because either would have shipped as a "finding":

1. **Prepared-statement plan reuse.** The exact and approximate queries are byte-identical
   SQL, differing only in a planner GUC. A cached plan is not invalidated by a GUC
   change, so the sequential-scan plan prepared for ground truth was reused for every
   subsequent measurement. The index path silently ran the exact path: latency matched to
   within noise and recall was a perfect 1.000. Both numbers were wrong, and both were
   plausible. The benchmark now runs on an engine with statement caching disabled.
2. **Buffer-pool eviction.** Ground truth scans the whole table, evicting the HNSW index
   from `shared_buffers`. Timing the index path immediately afterwards measures cold
   reads. Every configuration is run once untimed before it is measured.

The general lesson: a benchmark that reports exactly what you expected is not evidence.
Recall of 1.000 at every selectivity should have been suspicious on sight.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text

from gatekeeper.core.db import principal_session, unprepared_engine
from gatekeeper.core.models import Chunk
from gatekeeper.retrieval.search import coarse_predicate

if TYPE_CHECKING:
    from gatekeeper.core.principal import Principal
    from gatekeeper.llm.embeddings import Embedder

# `off` is the pre-0.8 behaviour and the baseline worth beating. `relaxed_order` keeps
# pulling from the graph until k visible rows are found, permitting slight mis-ordering.
# `strict_order` preserves ordering at more cost.
SCAN_MODES = ("off", "relaxed_order", "strict_order")
EF_VALUES = (40, 100, 200, 400)
DEFAULT_EF = 200

QUERIES = [
    "How much can I expense for a meal on a business trip?",
    "What is the equity refresh policy for executives?",
    "How do I report a security incident?",
    "What is the parental leave policy in the Netherlands?",
    "What is the board meeting cadence and who attends?",
    "How does the compensation review cycle work?",
    "Who approves a purchase over ten thousand dollars?",
    "What is the process for offboarding a team member?",
    "How does the vulnerability disclosure program work?",
    "What is the code review process for a merge request?",
]


@dataclass
class Measurement:
    principal: str
    selectivity: float
    scan_mode: str
    ef_search: int
    recall: float
    short_returns: int
    p50_ms: float
    p95_ms: float
    exact_p50_ms: float
    coarse: bool = True
    plan: str = ""


async def _selectivity(principal: Principal, corpus_size: int) -> float:
    async with principal_session(principal) as session:
        visible = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
    return visible / corpus_size if corpus_size else 0.0


async def _exact_topk(
    principal: Principal, vector: list[float], embedder: Embedder, k: int
) -> tuple[list[str], float]:
    """Brute-force top-k under the principal's own RLS policy.

    Index scans are disabled so the planner must compute every distance. This is the
    ground truth the approximate path is scored against; it must run as the principal,
    not as the owner, or it would be the top-k of a corpus the principal cannot see.
    """
    column = getattr(Chunk, embedder.space.column)
    started = time.monotonic()
    async with principal_session(principal, unprepared_engine()) as session:
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        await session.execute(text("SET LOCAL enable_bitmapscan = off"))
        rows = (
            await session.execute(
                select(Chunk.id)
                .where(column.is_not(None), Chunk.embedding_model == embedder.space.model)
                .order_by(column.cosine_distance(vector))
                .limit(k)
            )
        ).all()
    return [str(r.id) for r in rows], (time.monotonic() - started) * 1000


def _approx_stmt(
    principal: Principal, vector: list[float], embedder: Embedder, k: int, coarse: bool
) -> Any:
    column = getattr(Chunk, embedder.space.column)
    stmt = (
        select(Chunk.id)
        .where(column.is_not(None), Chunk.embedding_model == embedder.space.model)
        .where(Chunk.tenant_id == principal.tenant_id)
        .order_by(column.cosine_distance(vector))
        .limit(k)
    )
    if coarse:
        stmt = stmt.where(*coarse_predicate(Chunk, principal))
    return stmt


async def _approx_topk(
    principal: Principal,
    vector: list[float],
    embedder: Embedder,
    k: int,
    scan_mode: str,
    ef_search: int,
    coarse: bool = True,
) -> tuple[list[str], float]:
    started = time.monotonic()
    async with principal_session(principal, unprepared_engine()) as session:
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
        await session.execute(text(f"SET LOCAL hnsw.iterative_scan = '{scan_mode}'"))
        rows = (await session.execute(_approx_stmt(principal, vector, embedder, k, coarse))).all()
    return [str(r.id) for r in rows], (time.monotonic() - started) * 1000


async def _plan_of(
    principal: Principal,
    vector: list[float],
    embedder: Embedder,
    k: int,
    scan_mode: str,
    ef_search: int,
    coarse: bool,
) -> str:
    """The scan node the planner actually chose.

    The first version of this benchmark reported latency and left the plan to be inferred,
    which is how a cached sequential-scan plan passed for an index scan for two rounds.
    Reading it from EXPLAIN makes the claim checkable.
    """
    stmt = _approx_stmt(principal, vector, embedder, k, coarse)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    async with principal_session(principal, unprepared_engine()) as session:
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
        await session.execute(text(f"SET LOCAL hnsw.iterative_scan = '{scan_mode}'"))
        rows = (await session.execute(text("EXPLAIN " + compiled))).all()
    for row in rows:
        line = str(row[0]).strip()
        if "Scan" in line:
            node = line.lstrip("-> ").split("  (cost")[0]
            if "using ix_chunks_emb384_public" in node:
                return "HNSW (public partial)"
            if "using ix_chunks_emb384_internal" in node:
                return "HNSW (internal partial)"
            if "hnsw" in node:
                return "HNSW (full)"
            return node
    return "unknown"


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * q), len(ordered) - 1)]


async def run(
    principals: dict[str, Principal],
    embedder: Embedder,
    corpus_size: int,
    k: int = 10,
    queries: list[str] | None = None,
) -> list[Measurement]:
    probes = queries or QUERIES
    vectors = {q: embedder.encode_query(q).tolist() for q in probes}

    results: list[Measurement] = []
    for handle, principal in principals.items():
        if principal.is_expired:
            continue
        selectivity = await _selectivity(principal, corpus_size)

        # Ground truth once per (principal, query); it is the expensive half and does
        # not vary with the index settings being swept.
        truth: dict[str, list[str]] = {}
        exact_latencies: list[float] = []
        for query in probes:
            ids, elapsed = await _exact_topk(principal, vectors[query], embedder, k)
            truth[query] = ids
            exact_latencies.append(elapsed)
        exact_p50 = _percentile(exact_latencies, 0.5)

        # The sweep runs with the coarse predicate on (production behaviour). The
        # `coarse=False` pass at default settings is the before-and-after for ADR 0006,
        # measured on the same principal, corpus and queries within one run.
        for coarse in (False, True):
            modes = SCAN_MODES if coarse else ("relaxed_order",)
            efs = EF_VALUES if coarse else (DEFAULT_EF,)
            for scan_mode in modes:
                for ef_search in efs:
                    # Warmup: the ground-truth pass above flushed the buffer pool.
                    for query in probes:
                        await _approx_topk(
                            principal, vectors[query], embedder, k, scan_mode, ef_search, coarse
                        )

                    recalls: list[float] = []
                    latencies: list[float] = []
                    short = 0
                    for query in probes:
                        ids, elapsed = await _approx_topk(
                            principal, vectors[query], embedder, k, scan_mode, ef_search, coarse
                        )
                        latencies.append(elapsed)
                        expected = truth[query]
                        if not expected:
                            continue
                        if len(ids) < min(k, len(expected)):
                            short += 1
                        recalls.append(len(set(ids) & set(expected)) / len(expected))

                    plan = await _plan_of(
                        principal, vectors[probes[0]], embedder, k, scan_mode, ef_search, coarse
                    )
                    results.append(
                        Measurement(
                            principal=handle,
                            selectivity=selectivity,
                            scan_mode=scan_mode,
                            ef_search=ef_search,
                            recall=sum(recalls) / len(recalls) if recalls else 0.0,
                            short_returns=short,
                            p50_ms=_percentile(latencies, 0.5),
                            p95_ms=_percentile(latencies, 0.95),
                            exact_p50_ms=exact_p50,
                            coarse=coarse,
                            plan=plan,
                        )
                    )
    return results


def to_markdown(results: list[Measurement], k: int, corpus_size: int) -> str:
    """Render the benchmark as the table that goes in the README."""
    lines = [
        "# Filtered-ANN benchmark",
        "",
        f"Recall@{k} of the HNSW path against exact brute-force ground truth, both",
        "computed under the same row-level security policy, on a corpus of",
        f"{corpus_size:,} chunks (bge-small-en-v1.5, 384d, halfvec, m=16, ef_construction=64).",
        "",
        "**Selectivity** is the fraction of the corpus the principal may read. It is the",
        "variable that matters: the more a policy filters, the more of the HNSW candidate",
        "list is discarded before it can be ranked.",
        "",
        "## Findings",
        "",
        "**1. The selectivity cliff is fixed, and mostly not by the thing built to fix it.**",
        "",
        "Phase 2 measured a 79x latency cliff: at 5.2% selectivity Postgres chose a parallel",
        "sequential scan, at 84.6% it used the HNSW index. The access policy, not the query,",
        "decided which. Recall stayed at 1.000 throughout -- a sequential scan is exact -- so",
        "nothing in the results hinted that anything had changed.",
        "",
        "Isolating the cause by downgrading and re-running against the same corpus:",
        "",
        "| `authorize()` form | guest plan | guest p50 | raj p50 |",
        "|---|---|---:|---:|",
        "| Phase 2: `STABLE`, called per row | `Parallel Seq Scan` | 79.0 ms | 2.2 ms |",
        "| ADR 0007: `IMMUTABLE`, inlinable | `HNSW (full)` | 6.1 ms | 2.2 ms |",
        "| + coarse predicate & partial index | `HNSW (public partial)` | 2.0 ms | 2.2 ms |",
        "",
        "**13x of the 40x came from ADR 0007**, which was written to fix lexical-query latency",
        "and had nothing to do with this. An opaque `STABLE` function gives the planner a",
        "default selectivity guess that made the sequential scan look cheap; making the",
        "predicate `IMMUTABLE` and inlinable let it fold the clauses into the query's quals and",
        "estimate them properly. The cliff was a symptom of the same root cause as the 11x",
        "lexical slowdown, and neither diagnosis saw that at the time.",
        "",
        "The remaining 3x is this phase's work: `coarse_predicate()` restates two of the",
        "policy's own clauses in the query so the planner can index them, and migration 0008",
        "adds partial HNSW graphs for the `public` and `public+internal` tiers. The public",
        "graph is 4.4 MB against 81 MB for the full one, so the most restricted principal now",
        "searches ~5% of the structure. Principals above ~80% selectivity are unaffected,",
        "which is the intended outcome -- they were never on the wrong side of the cliff.",
        "",
        "Worth stating plainly: ADR 0006 proposed **per-tenant** partial indexes. That was the",
        "wrong axis. The cliff is intra-tenant -- `guest` and `mira` share a tenant and differ",
        "18x in what they can read -- so a per-tenant index would have helped neither.",
        "",
        "**2. `iterative_scan` fixes short returns; it does not fix recall.** At `ef_search=40`",
        "with iterative scan `off`, up to 3 of 10 queries return fewer than k rows. Turning it",
        "on eliminates short returns entirely -- which is what Phase 1 observed and wrongly",
        "read as sufficient. Recall at that setting is still 0.79-0.94: the results look",
        "complete and are quietly wrong. `ef_search >= 200` reaches 1.000 for every principal",
        "at a cost of ~1 ms.",
        "",
        "**3. Where the index is used, it is worth 60x.** 4-6 ms against 280-330 ms for exact",
        "brute force over the same 73,797 chunks under the same policy.",
        "",
        "## Full sweep",
        "",
        f"| principal | selectivity | iterative_scan | ef_search | recall@{k} | short "
        "| p50 | p95 | exact p50 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for m in results:
        if not m.coarse:
            continue
        lines.append(
            f"| {m.principal} | {m.selectivity:.1%} | `{m.scan_mode}` | {m.ef_search} | "
            f"{m.recall:.3f} | {m.short_returns} | {m.p50_ms:.0f} ms | {m.p95_ms:.0f} ms | "
            f"{m.exact_p50_ms:.0f} ms |"
        )
    return "\n".join(lines) + "\n"


def before_after_markdown(results: list[Measurement], k: int) -> str:
    """The ADR 0006 fix, measured on the same principals and queries in one run."""
    before = {m.principal: m for m in results if not m.coarse}
    after = {
        m.principal: m
        for m in results
        if m.coarse and m.scan_mode == "relaxed_order" and m.ef_search == DEFAULT_EF
    }

    lines = [
        "## The selectivity cliff, before and after",
        "",
        "`coarse_predicate()` restates two of the RLS policy's own clauses in the query —",
        "`min_clearance <= clearance` and `sensitivity = 'public' OR allowed_groups && groups`",
        "— so the planner can estimate and index them, and migration 0008 adds partial HNSW",
        "graphs for the `public` and `public+internal` tiers those clauses can match.",
        "",
        "Both columns are the same principal, corpus, queries and settings",
        f"(`relaxed_order`, `ef_search={DEFAULT_EF}`), measured in a single run. `recall` is",
        "against exact brute-force ground truth computed under the same policy.",
        "",
        f"| principal | selectivity | before: plan / p50 / recall@{k} "
        f"| after: plan / p50 / recall@{k} |",
        "|---|---:|---|---|",
    ]
    for handle, b in before.items():
        a = after.get(handle)
        if a is None:
            continue
        lines.append(
            f"| {handle} | {b.selectivity:.1%} "
            f"| `{b.plan}` · {b.p50_ms:.0f} ms · {b.recall:.3f} "
            f"| `{a.plan}` · {a.p50_ms:.0f} ms · {a.recall:.3f} |"
        )
    return "\n".join(lines) + "\n"
