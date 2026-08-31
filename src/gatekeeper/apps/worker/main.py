"""Arq worker settings.

Redis has been in `compose.yml` since Phase 0 and unused until now; this is what it is
for. Postgres holds the job *state* (queryable, durable, survives a Redis flush) and Redis
holds the *queue* — the split matters because "what is the status of the reindex" is a
question an operator asks long after the message is gone.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from arq.connections import RedisSettings

from gatekeeper.apps.worker.tasks import ingest_documents, shutdown, startup
from gatekeeper.config import get_settings


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


class WorkerSettings:
    functions = [ingest_documents]  # noqa: RUF012 - arq reads these as plain attributes
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    # Embedding is CPU-bound and already uses every core, so a second concurrent job
    # would contend rather than parallelise.
    max_jobs = 1
    # Long enough for the full corpus. arq kills a job at this deadline, and a reindex
    # cut off at 30 minutes would be worse than one that takes 45.
    job_timeout = 7200
    # Retries are for transient infrastructure -- the failure modes that motivated this
    # worker. A document that fails deterministically is recorded per-document instead.
    max_tries = 3
    keep_result = 3600


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - process entry point
    from arq import run_worker

    logging.basicConfig(stream=sys.stderr, level=get_settings().log_level)
    run_worker(WorkerSettings)  # type: ignore[arg-type]


def worker_settings() -> dict[str, Any]:
    return {"max_jobs": WorkerSettings.max_jobs, "job_timeout": WorkerSettings.job_timeout}
