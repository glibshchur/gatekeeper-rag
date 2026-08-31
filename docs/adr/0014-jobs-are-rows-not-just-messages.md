# 0014 — Ingestion jobs are rows; the queue only carries the id

**Status:** Accepted · **Date:** 2026-08-31 · **Phase:** 5

## Context

Indexing the handbook takes about forty minutes and was a foreground command. Three
things went wrong with that during development, and none of them were hypothetical:

* A MinIO clock-skew error and a stalled Hugging Face fetch each destroyed a
  twenty-five-minute run outright.
* There was no progress. The only way to know how far a run had got was to count rows in
  Postgres from a second terminal.
* Nothing could retry anything smaller than everything.

Redis has been in `compose.yml` since Phase 0 and unused since Phase 0.

## Decision

`arq` over Redis, with one structural choice: **the queue message contains only a job id.
Every parameter, and all state, lives in an `ingest_jobs` row.**

The task reads its tenant, paths and `force` flag from the row, and writes `status`,
`attempts`, `total`, `done`, `chunks_written` and `failures` back to it. Redis carries a
UUID and nothing else.

This is the opposite of the usual arrangement, where the payload is the message and the
database is optional. It is chosen because of who debugs this. An operator looking at a
stuck reindex should be able to answer "what is it doing, how far has it got, and what
failed" with a `SELECT`. If the parameters live in the message, answering that means
decoding a Redis value, and if Redis has been flushed — which is exactly what happens
after the kind of incident that leaves a job stuck — the answer is gone.

The enqueue writes the row *before* the message. A crash between the two leaves a visible
`queued` job that can be re-submitted; the other order leaves work running that nothing
knows about.

**The dead-letter queue is a column, not a second system.** `failures` is a JSONB array of
`{path, error}`. `gatekeeper jobs retry <id>` reads it and enqueues a new job for exactly
those paths. A separate DLQ topic would need its own retention, its own inspection tooling
and its own correlation back to the original job; a column has all three for free, and the
retry is cheap because the pipeline already skips unchanged documents.

**Failure isolation is a transaction boundary, not a `try` block.** Each document gets its
own `admin_session`. Wrapping the loop in one transaction and catching exceptions would
still roll back every committed document when a later one failed — which is what made the
foreground path lose twenty-five minutes of work to a single bad blob upload.

## Consequences

Writing progress costs one `UPDATE` per ten documents (`PROGRESS_EVERY`). Per-document
would be 4,586 extra writes for a number a human reads every few seconds.

`max_jobs=1`. Embedding is CPU-bound and in-process; running two jobs per worker would
make both slower and neither more parallel. Horizontal scale is more worker processes.

**A silent skip had to become a failure.** `pipeline.index_one` returned `False` both for
"unchanged, nothing to do" and for "the file is gone from the clone". In the foreground
that was tolerable — both increment `documents_skipped`, and a human reads the log. In a
background job it meant a run reporting **`succeeded` while a document went unindexed**,
which is the one thing a job must never do quietly. `IndexReport` now counts
`missing_sources` separately and the worker turns each one into a failure entry, so it
lands in the retry set. This was found by deliberately hiding a source file and watching
the job report success — not by reading the code.

**No worker service in `compose.yml`.** Compose holds infrastructure only; the application
runs on the host under `uv`, and a worker image would have to carry the ONNX models. `make
worker` is consistent with `make ui` and `make mcp`. If this ever grows a Dockerfile, the
worker is the first service that should use it.
