"""Background ingestion, with per-document failure isolation.

Ingestion is the only genuinely slow thing this system does — 40 minutes to embed the
handbook — and it was previously a foreground command. That is survivable for a demo and
wrong for anything else, for three reasons that all showed up during development:

1. **A transient error killed the whole batch.** A MinIO clock-skew error and a stalled
   Hugging Face fetch each destroyed a 25-minute run. Here a document that fails is
   recorded and the batch continues.
2. **There was no progress.** The only way to know how far a run had got was to count
   rows in Postgres from another terminal.
3. **Nothing could retry.** Re-running meant redoing everything, or hand-writing a
   targeted repair command — which is what `index repair` became.

The work itself is unchanged: `ingest.pipeline` already skips documents whose content and
embedding space are unchanged, so a retry costs one query per untouched document.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from gatekeeper.config import get_settings
from gatekeeper.core import telemetry
from gatekeeper.core.db import admin_session, dispose_engines
from gatekeeper.core.models import Document, IngestJob, Tenant
from gatekeeper.ingest import handbook, pipeline
from gatekeeper.ingest.acl import load_rules
from gatekeeper.llm.embeddings import build_embedder

logger = logging.getLogger(__name__)

# How often progress reaches the database. Every document would be 4,586 extra writes for
# a number a human reads once every few seconds.
PROGRESS_EVERY = 10


async def _set(job_id: UUID, **values: Any) -> None:
    async with admin_session() as session:
        await session.execute(update(IngestJob).where(IngestJob.id == job_id).values(**values))


async def ingest_documents(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Chunk and embed the documents named by a job.

    Reads its own parameters from the job row rather than the arq payload, so a job
    remains inspectable and retryable from SQL alone — an operator debugging a stuck
    reindex should not have to decode a Redis message to find out what it was doing.
    """
    jid = UUID(job_id)
    settings = get_settings()

    async with admin_session() as session:
        job = (await session.execute(select(IngestJob).where(IngestJob.id == jid))).scalar_one()
        payload = dict(job.payload)
        attempts = job.attempts + 1
        # Only the paths named by the job: on a retry that is the previous failures, not
        # the whole corpus.
        paths: list[str] = list(payload.get("paths") or [])
        tenant_slug = str(payload.get("tenant") or "")
        force = bool(payload.get("force"))

    await _set(
        jid,
        status="running",
        attempts=attempts,
        started_at=datetime.now(UTC),
        error=None,
        failures=[],
        done=0,
    )

    embedder = ctx.get("embedder") or build_embedder(
        settings.embedding_backend, settings.openai_api_key
    )
    rules = load_rules(settings.acl_rules_path)
    content_root = settings.corpus_dir / handbook.SOURCE / "content"

    async with admin_session() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == tenant_slug))
        ).scalar_one()
        stmt = select(Document).where(Document.tenant_id == tenant_id).order_by(Document.path)
        if paths:
            stmt = stmt.where(Document.path.in_(paths))
        documents = list((await session.execute(stmt)).scalars())

    await _set(jid, total=len(documents))

    report = pipeline.IndexReport()
    failures: list[dict[str, str]] = []
    done = 0

    for document in documents:
        missing_before = report.missing_sources
        try:
            async with admin_session() as session:
                # Re-attached per document so one rollback cannot discard the batch's
                # committed work -- the transaction boundary *is* the isolation.
                merged = await session.merge(document, load=False)
                await pipeline.index_one(
                    session=session,
                    document=merged,
                    tenant_slug=tenant_slug,
                    content_root=content_root,
                    embedder=embedder,
                    rules=rules,
                    target_tokens=int(embedder.space.max_tokens * 0.75),
                    overlap_tokens=64,
                    force=force,
                    report=report,
                )
        except Exception as exc:
            logger.warning("ingest failed for %s: %s", document.path, exc)
            failures.append({"path": document.path, "error": f"{type(exc).__name__}: {exc}"})
        else:
            # A missing source file is not an exception -- `index_one` logs it and returns
            # False, the same value it returns for an unchanged document. Left at that, a
            # job would report `succeeded` while a document went unindexed, which is the
            # one outcome a background job must never report quietly. The counter delta is
            # the only thing that distinguishes the two, so it becomes a failure here and
            # lands in the retry set.
            if report.missing_sources > missing_before:
                failures.append(
                    {
                        "path": document.path,
                        "error": "FileNotFoundError: source file missing from the clone",
                    }
                )

        done += 1
        if done % PROGRESS_EVERY == 0 or done == len(documents):
            await _set(jid, done=done, chunks_written=report.chunks_written, failures=failures)

    status = "succeeded" if not failures else "partial"
    await _set(
        jid,
        status=status,
        done=done,
        chunks_written=report.chunks_written,
        failures=failures,
        finished_at=datetime.now(UTC),
    )
    return {
        "status": status,
        "documents": done,
        "chunks": report.chunks_written,
        "failures": len(failures),
    }


async def startup(ctx: dict[str, Any]) -> None:
    """Load the embedding model once per worker, not once per job.

    An 18-second model load per job would dominate everything and make the queue look
    broken under any real volume.
    """
    settings = get_settings()
    if telemetry.configure():
        logger.info("tracing to %s", settings.otel_endpoint)
    logger.info("loading embedder %s", settings.embedding_backend)
    ctx["embedder"] = build_embedder(settings.embedding_backend, settings.openai_api_key)


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engines()
