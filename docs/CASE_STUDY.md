# Case study copy

Ready-to-paste text for an Upwork profile, a portfolio page, or a proposal. Written to
lead with **outcomes and measurements**, not the stack — the stack is commodity, the
measurements are not.

Replace `github.com/glibshchur/gatekeeper-rag` with the real URL, and the video placeholder once
recorded ([shot list](DEMO_SCRIPT.md)).

---

## Upwork profile headline (70 chars)

> Permission-aware RAG & LLM systems — measured, red-teamed, production-shaped

## Upwork profile summary (short)

> I build retrieval systems for companies where **not everyone may see every document**.
>
> Most RAG demos answer questions well and handle access control by filtering results in
> application code after retrieval. That's the wrong layer — one bug, one injected prompt,
> or one compromised process and a restricted document is already in memory.
>
> My reference implementation pushes authorization into PostgreSQL row-level security, so
> the database refuses to return rows the caller isn't cleared to see. Measured results
> over a 73,801-chunk corpus:
>
> - **0 leaks** across 360 adversarial probes
> - **0 disagreements** between the database and an independent policy implementation, across all 442,782 (principal, chunk) pairs
> - **0 of 13** indirect prompt injections that reached the model widened access — including 2 that defeated the classifier entirely
> - **nDCG@10 0.793** (±0.003 across runs) on a hand-written golden set
> - **1,207 authorized queries/sec** at concurrency 64
>
> Everything is reproducible: `docker compose up`, one command, no API keys required.
>
> Code, benchmarks and 16 architecture decision records: github.com/glibshchur/gatekeeper-rag

## Portfolio project description (long)

> ### gatekeeper-rag — enterprise RAG where the database enforces access control
>
> **The problem.** Internal knowledge bases contain material most employees shouldn't
> read: compensation, board minutes, security runbooks, per-country employment terms. The
> standard RAG architecture retrieves first and filters second, which means restricted
> content leaves the database before anything decides it shouldn't have. Every bug between
> the query and the filter is a disclosure.
>
> **What I built.** A permission-aware RAG platform over the GitLab handbook (4,586
> documents, 73,801 chunks) with a plausible enterprise access model layered onto its real
> departmental structure. Authorization is attribute-based — groups, clearance,
> need-to-know, jurisdiction, expiry, deny-overrides-allow — compiled into a single SQL
> policy that runs *inside* the vector scan. The query layer connects as a role that
> cannot bypass it.
>
> **How I know it works.** An independent Python implementation of the same written
> specification is reconciled against the database over every (principal, chunk) pair.
> Asking the database whether the database got it right proves nothing; two
> implementations built from one spec disagreeing loudly is the point. 442,782 pairs, 0
> disagreements. Plus 360 adversarial probes across 8 categories: 0 leaks.
>
> **Indirect prompt injection.** 19 payloads planted as readable documents in the live
> corpus and attacked through the real pipeline. 13 reached the model, 0 widened access —
> including 2 the classifier missed completely. Containment is structural: entitlements
> live in a transaction-local database setting that nothing the model emits can write. An
> injection can persuade the model of anything and still not make the database return a
> row.
>
> **Retrieval quality, measured honestly.** 58 hand-written questions (never generated
> from chunk text — that makes the eval circular), a stage-by-stage ablation whose arms
> differ only in configuration, and published variance bands. nDCG@10 0.793. Hybrid search
> was built, measured at indistinguishable from dense+rerank across four runs, for 66% more
> latency, and **turned off**, reported as prominently as the wins.
>
> **Production shape.** Background ingestion with per-document failure isolation and
> targeted retry; bearer-token auth (HS256 dev / RS256 OIDC); OpenTelemetry tracing
> designed so spans carry shapes and never contents, because a trace store is usually a
> weaker-access copy of exactly what you're protecting; a load sweep that showed the
> bottleneck is the embedding model, not the authorization.
>
> **Stack.** PostgreSQL 16 + pgvector (halfvec, HNSW, partial indexes), FastAPI,
> SQLAlchemy 2.0 async, Alembic, arq, ONNX Runtime, MCP, OpenTelemetry, Docker Compose.
> Python 3.13, ruff + mypy strict, 253 tests.
>
> **16 architecture decision records**, including the ones where I was wrong.

## Proposal snippet (cold outreach / job application)

> You're building RAG over documents that not everyone should see. The part that usually
> goes wrong isn't retrieval quality — it's that access control gets implemented as a
> filter applied *after* the search, so a prompt injection or an ordinary bug turns into a
> disclosure.
>
> I've built and red-teamed exactly this: authorization enforced inside PostgreSQL
> row-level security, verified against an independent implementation of the policy across
> 442,782 principal/document pairs with zero disagreements, and tested against indirect
> prompt injections planted in the live corpus — none of which widened access, including
> the ones my own classifier failed to detect.
>
> Reference implementation with full benchmarks: github.com/glibshchur/gatekeeper-rag
>
> Happy to walk through the threat model and where it's still weak — it's documented.

## The 30-second verbal version

> Most RAG systems filter search results in Python to enforce permissions. That means the
> restricted document left the database before anything checked. I push the access policy
> into Postgres row-level security so the database won't return it at all — and then I
> prove it, by writing a second independent implementation of the policy and reconciling
> the two across every user-document pair. Zero disagreements out of 442,782. Then I
> planted prompt injections in the live corpus to try to break it. Thirteen reached the
> model. None got anything.

---

## Talking points for a technical interview

Pick based on what they seem to care about. Each has a real number and, more usefully, a
place where it went wrong.

**"Tell me about a hard debugging problem."** The selectivity cliff — the most restricted
users were 36× slower than everyone else, silently, because below a selectivity threshold
Postgres abandons the vector index. Nothing in the results indicates it; recall stays
perfect because a sequential scan is exact. The fix I proposed in the ADR was on the wrong
axis, and 13× of the eventual 40× came from a change I'd made weeks earlier for an
unrelated reason. Both investigations shared a root cause neither had named.

**"Tell me about a time you were wrong."** Several, all documented. I built hybrid search
and turned it off. I built a semantic layer for the injection classifier, measured that it
could not have worked at any threshold, and deleted it. I wrote a benchmark that reported
recall 1.000 everywhere and believed it for a while — it was prepared-statement plan reuse
across a planner setting change.

**"How do you know your security works?"** I don't trust a system to grade itself. There's
a separate Python implementation of the same written spec, and the two are reconciled over
every (principal, chunk) pair. When they disagree, one of them is wrong and I have to find
out which.

**"What's still broken?"** No token revocation. The API process holds an RLS-bypassing
connection for the withheld-count baseline, which means a code-execution compromise of
that process isn't bounded by the policy at all — that's the largest gap between what the
split-privilege design claims and what it delivers. Timing side channels aren't mitigated.
All of it is in the threat model rather than omitted from it.
