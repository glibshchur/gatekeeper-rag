"""Chunk, embed, and index the documents already registered by the corpus loader.

Runs on the admin plane. Ingestion writes the denormalised effective ACL onto every chunk
it creates, which is what lets the Phase 1 retrieval query filter inside the vector scan
instead of after it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeeper.core.db import admin_session
from gatekeeper.core.models import Chunk, Document, Tenant
from gatekeeper.ingest import blobs
from gatekeeper.ingest.chunking import chunk_markdown
from gatekeeper.llm.embeddings import Embedder

logger = logging.getLogger(__name__)


@dataclass
class IndexReport:
    documents_seen: int = 0
    documents_indexed: int = 0
    documents_skipped: int = 0
    chunks_written: int = 0
    oversized_chunks: int = 0
    blobs_written: int = 0

    def as_rows(self) -> list[tuple[str, str]]:
        return [
            ("documents seen", f"{self.documents_seen:,}"),
            ("documents indexed", f"{self.documents_indexed:,}"),
            ("documents skipped (unchanged)", f"{self.documents_skipped:,}"),
            ("chunks written", f"{self.chunks_written:,}"),
            ("oversized chunks", f"{self.oversized_chunks:,}"),
            ("blobs written", f"{self.blobs_written:,}"),
        ]


async def build_index(
    *,
    embedder: Embedder,
    clone_dir: Path,
    tenant_slug: str,
    target_tokens: int | None = None,
    overlap_tokens: int = 64,
    limit: int | None = None,
    force: bool = False,
    batch_documents: int = 25,
) -> IndexReport:
    """Chunk and embed every document in the tenant that needs it.

    Chunk size defaults to three quarters of the backend's context window, leaving room
    for the heading prefix. Hardcoding 1000 tokens here -- the number the previous
    incarnation of this project used -- would silently truncate every chunk under the
    default local backend, whose context is 512.
    """
    content_root = clone_dir / "content"
    space = embedder.space
    target = target_tokens or int(space.max_tokens * 0.75)
    if target > space.max_tokens:
        raise ValueError(f"target_tokens={target} exceeds {space.model}'s {space.max_tokens}")

    report = IndexReport()
    bucket = blobs.ensure_bucket()
    logger.info(
        "indexing into %s (dim=%d, target=%d tokens, bucket=%s)",
        space.model,
        space.dim,
        target,
        bucket,
    )

    async with admin_session() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == tenant_slug))
        ).scalar_one()
        stmt = select(Document).where(Document.tenant_id == tenant_id).order_by(Document.path)
        if limit:
            stmt = stmt.limit(limit)
        documents = list((await session.execute(stmt)).scalars())

    report.documents_seen = len(documents)

    for start in range(0, len(documents), batch_documents):
        window = documents[start : start + batch_documents]
        async with admin_session() as session:
            for document in window:
                written = await _index_one(
                    session=session,
                    document=document,
                    tenant_slug=tenant_slug,
                    content_root=content_root,
                    embedder=embedder,
                    target_tokens=target,
                    overlap_tokens=overlap_tokens,
                    force=force,
                    report=report,
                )
                if written:
                    report.documents_indexed += 1
                else:
                    report.documents_skipped += 1
        logger.info(
            "indexed %d/%d documents (%d chunks)",
            min(start + batch_documents, len(documents)),
            len(documents),
            report.chunks_written,
        )

    return report


async def _index_one(
    *,
    session: AsyncSession,
    document: Document,
    tenant_slug: str,
    content_root: Path,
    embedder: Embedder,
    target_tokens: int,
    overlap_tokens: int,
    force: bool,
    report: IndexReport,
) -> bool:
    space = embedder.space

    # Incremental reindex: skip a document whose bytes and embedding space are both
    # unchanged. This is what makes iterating on chunk parameters cheap -- change
    # `target_tokens` and pass --force, change nothing and re-running costs one query.
    if not force:
        existing = (
            await session.execute(
                select(func.count(Chunk.id)).where(
                    Chunk.document_id == document.id, Chunk.embedding_model == space.model
                )
            )
        ).scalar_one()
        if existing:
            return False

    source_file = content_root / document.path
    if not source_file.is_file():
        logger.warning("missing source file for %s", document.path)
        return False

    raw = source_file.read_bytes()
    key = blobs.blob_key(tenant_slug, document.source, document.content_hash)
    if not blobs.exists(key):
        blobs.put(key, raw)
        report.blobs_written += 1

    body = raw.decode("utf-8", errors="replace")
    text_chunks = chunk_markdown(
        body,
        embedder.count_tokens,
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
        title=document.title,
    )
    if not text_chunks:
        return False

    vectors = embedder.encode_passages([c.content for c in text_chunks])

    await session.execute(
        delete(Chunk).where(Chunk.document_id == document.id, Chunk.embedding_model == space.model)
    )

    column = space.column
    for text_chunk, vector in zip(text_chunks, vectors, strict=True):
        chunk = Chunk(
            tenant_id=document.tenant_id,
            document_id=document.id,
            ordinal=text_chunk.ordinal,
            content=text_chunk.content,
            heading_path=text_chunk.heading_path,
            token_count=text_chunk.token_count,
            # The effective ACL is copied from the document. Chunk-level overrides land
            # in Phase 2; until then every chunk is `inherited` and says so.
            sensitivity=document.sensitivity,
            allowed_groups=list(document.allowed_groups),
            min_clearance=document.min_clearance,
            acl_source="inherited",
            embedding_model=space.model,
        )
        setattr(chunk, column, vector.tolist())
        session.add(chunk)
        report.chunks_written += 1
        if text_chunk.oversized:
            report.oversized_chunks += 1

    return True


def summarise_spaces(counts: Sequence[tuple[str, int]]) -> str:
    return ", ".join(f"{model}={n:,}" for model, n in counts) or "none"
