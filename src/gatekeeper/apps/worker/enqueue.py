"""Creating jobs. Separated from the worker so the API and CLI can enqueue without
importing arq's worker machinery."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from arq import create_pool
from sqlalchemy import select

from gatekeeper.apps.worker.main import redis_settings
from gatekeeper.core.db import admin_session
from gatekeeper.core.models import IngestJob

if TYPE_CHECKING:
    from uuid import UUID


async def enqueue_ingest(
    tenant_slug: str, paths: list[str] | None = None, force: bool = False
) -> UUID:
    """Create a job row, then hand its id to the queue.

    Row first, deliberately. If the enqueue fails the job is visible as `queued` and can
    be re-submitted; if the row were written second, a crash between the two would leave
    work running that nothing knows about.
    """
    job_id = uuid.uuid4()
    async with admin_session() as session:
        session.add(
            IngestJob(
                id=job_id,
                kind="ingest",
                payload={"tenant": tenant_slug, "paths": paths or [], "force": force},
                status="queued",
            )
        )

    pool = await create_pool(redis_settings())
    try:
        await pool.enqueue_job("ingest_documents", str(job_id))
    finally:
        await pool.aclose()
    return job_id


async def retry_failures(job_id: UUID) -> UUID | None:
    """Re-enqueue only the documents that failed. Returns the new job id, or None.

    This is the dead-letter queue being drained. Because the paths come from the previous
    job's `failures`, a retry after a transient outage costs one query per document that
    already succeeded -- the pipeline skips unchanged work -- rather than a full reindex.
    """
    async with admin_session() as session:
        job = (await session.execute(select(IngestJob).where(IngestJob.id == job_id))).scalar_one()
        paths = [f["path"] for f in job.failures if isinstance(f, dict) and f.get("path")]
        payload = dict(job.payload)
    if not paths:
        return None
    return await enqueue_ingest(str(payload.get("tenant", "")), paths=paths, force=True)
