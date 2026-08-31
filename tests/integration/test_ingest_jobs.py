"""The background ingestion path, exercised without a running worker.

`ingest_documents` is called directly rather than through arq. What is worth testing is
the task's own contract -- failure isolation, progress, status, the retry set -- and
routing that through Redis would test arq's delivery instead, slowly and flakily.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select

from gatekeeper.apps.worker.enqueue import retry_failures
from gatekeeper.apps.worker.tasks import ingest_documents
from gatekeeper.core.db import admin_session
from gatekeeper.core.models import Document, IngestJob, Tenant

if TYPE_CHECKING:
    from tests.integration.conftest import Fixture

pytestmark = pytest.mark.integration

_created: list[uuid.UUID] = []


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """`retry_failures` enqueues for real. Left unpatched, these tests publish to whatever
    Redis is configured — and a worker running locally picks the job up and processes it
    against a scratch tenant. That happened. The row-writing half is what is under test;
    the publish is arq's job.
    """

    class _Pool:
        async def enqueue_job(self, *args: object, **kwargs: object) -> None:
            return None

        async def aclose(self) -> None:
            return None

    async def _create_pool(*args: object, **kwargs: object) -> _Pool:
        return _Pool()

    monkeypatch.setattr("gatekeeper.apps.worker.enqueue.create_pool", _create_pool)


@pytest.fixture(autouse=True)
async def clean_jobs() -> AsyncIterator[None]:
    """Job rows are not owned by a tenant, so the `fx` teardown does not reach them."""
    yield
    if _created:
        async with admin_session() as session:
            await session.execute(delete(IngestJob).where(IngestJob.id.in_(_created)))
        _created.clear()


class _StubEmbedder:
    """Deterministic vectors. The task's behaviour under failure is the subject here, and
    a real ONNX session would add 18 seconds per test to measure nothing."""

    class space:  # noqa: N801
        model = "bge-small-en-v1.5"
        column = "embedding_384"
        dim = 384
        max_tokens = 512

    def count_tokens(self, text: str) -> int:
        return max(1, len(text.split()))

    def encode_passages(self, texts: list[str]):  # type: ignore[no-untyped-def]
        import numpy as np

        out = np.zeros((len(texts), 384), dtype="float32")
        out[:, 0] = 1.0
        return out


async def _tenant_slug(tenant_id: uuid.UUID) -> str:
    async with admin_session() as session:
        return (
            await session.execute(select(Tenant.slug).where(Tenant.id == tenant_id))
        ).scalar_one()


async def _make_job(tenant_slug: str, paths: list[str]) -> uuid.UUID:
    job_id = uuid.uuid4()
    async with admin_session() as session:
        session.add(
            IngestJob(
                id=job_id,
                kind="ingest",
                payload={"tenant": tenant_slug, "paths": paths, "force": True},
                status="queued",
            )
        )
    _created.append(job_id)
    return job_id


async def _load(job_id: uuid.UUID) -> IngestJob:
    async with admin_session() as session:
        return (await session.execute(select(IngestJob).where(IngestJob.id == job_id))).scalar_one()


async def _add_document(tenant_id: uuid.UUID, path: str) -> uuid.UUID:
    doc_id = uuid.uuid4()
    async with admin_session() as session:
        session.add(
            Document(
                id=doc_id,
                tenant_id=tenant_id,
                source="test",
                path=path,
                title=path,
                content_hash=uuid.uuid4().hex * 2,
                sensitivity="internal",
                allowed_groups=["all-employees"],
                min_clearance=1,
            )
        )
    return doc_id


@pytest.fixture
def corpus_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the task at a scratch clone so the real corpus is never touched."""
    from gatekeeper.config import get_settings
    from gatekeeper.ingest import handbook

    content = tmp_path / handbook.SOURCE / "content"
    content.mkdir(parents=True)
    settings = get_settings()
    monkeypatch.setattr(settings, "corpus_dir", tmp_path, raising=False)
    return content


async def test_one_bad_document_does_not_discard_the_batch(fx: Fixture, corpus_root: Path) -> None:
    """The reason this task exists: a transient failure used to cost the whole run."""
    slug = await _tenant_slug(fx.tenant_a)
    for name in ("good-one.md", "good-two.md"):
        (corpus_root / name).write_text(f"# {name}\n\nSome ordinary handbook prose here.\n")
        await _add_document(fx.tenant_a, name)
    # Deliberately no file on disk for this one.
    await _add_document(fx.tenant_a, "vanished.md")

    job_id = await _make_job(slug, ["good-one.md", "good-two.md", "vanished.md"])
    result = await ingest_documents({"embedder": _StubEmbedder()}, str(job_id))

    assert result["status"] == "partial"
    job = await _load(job_id)
    assert job.status == "partial"
    assert job.done == 3, "every document is attempted, including those after the failure"
    assert job.chunks_written > 0, "the two good documents committed their work"
    assert [f["path"] for f in job.failures] == ["vanished.md"]


async def test_a_missing_source_is_a_failure_not_a_skip(fx: Fixture, corpus_root: Path) -> None:
    """`index_one` returns False for both 'unchanged' and 'file is gone'. Conflating them
    let a job report success while a document silently went unindexed."""
    slug = await _tenant_slug(fx.tenant_a)
    await _add_document(fx.tenant_a, "vanished.md")

    job_id = await _make_job(slug, ["vanished.md"])
    await ingest_documents({"embedder": _StubEmbedder()}, str(job_id))

    job = await _load(job_id)
    assert job.status == "partial"
    assert job.failures and "FileNotFoundError" in job.failures[0]["error"]


async def test_progress_reaches_the_database_before_the_job_ends(
    fx: Fixture, corpus_root: Path
) -> None:
    slug = await _tenant_slug(fx.tenant_a)
    (corpus_root / "a.md").write_text("# A\n\nProse.\n")
    await _add_document(fx.tenant_a, "a.md")

    job_id = await _make_job(slug, ["a.md"])
    await ingest_documents({"embedder": _StubEmbedder()}, str(job_id))

    job = await _load(job_id)
    assert job.total == 1
    assert job.done == 1
    assert job.progress == 1.0
    assert job.started_at is not None and job.finished_at is not None
    assert job.attempts == 1


async def test_retry_enqueues_only_what_failed(fx: Fixture, corpus_root: Path) -> None:
    """The dead-letter drain. A retry after an outage must not cost a full reindex."""
    slug = await _tenant_slug(fx.tenant_a)
    (corpus_root / "fine.md").write_text("# Fine\n\nProse.\n")
    await _add_document(fx.tenant_a, "fine.md")
    await _add_document(fx.tenant_a, "vanished.md")

    job_id = await _make_job(slug, ["fine.md", "vanished.md"])
    await ingest_documents({"embedder": _StubEmbedder()}, str(job_id))

    new_id = await retry_failures(job_id)
    assert new_id is not None
    _created.append(new_id)
    retried = await _load(new_id)
    assert retried.payload["paths"] == ["vanished.md"], "only the failure, not the batch"
    assert retried.status == "queued"


async def test_retry_of_a_clean_job_is_a_no_op(fx: Fixture, corpus_root: Path) -> None:
    slug = await _tenant_slug(fx.tenant_a)
    (corpus_root / "fine.md").write_text("# Fine\n\nProse.\n")
    await _add_document(fx.tenant_a, "fine.md")

    job_id = await _make_job(slug, ["fine.md"])
    await ingest_documents({"embedder": _StubEmbedder()}, str(job_id))
    assert (await _load(job_id)).status == "succeeded"
    assert await retry_failures(job_id) is None
