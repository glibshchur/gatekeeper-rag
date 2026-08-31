"""SQLAlchemy models.

Two conventions worth knowing before reading further:

1. ``tenant_id`` is on every row-bearing table and is part of every RLS policy. There is
   no code path that queries without it.
2. ``chunks`` carries a *denormalised* effective ACL (``allowed_groups``,
   ``min_clearance``, ``sensitivity``) rather than joining to ``documents``. Vector
   search filters on those columns inside the ANN scan, and a join there would defeat
   the index. ``acl_source`` records whether the values were inherited or overridden so
   the admin console can still show provenance.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import FetchedValue

SENSITIVITY_VALUES = ("public", "internal", "confidential", "restricted")


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _now()


class PrincipalRow(Base):
    __tablename__ = "principals"
    __table_args__ = (UniqueConstraint("tenant_id", "external_id", name="uq_principal_external"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")

    groups: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    clearance: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    department: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str] = mapped_column(Text, nullable=False, default="employee")
    need_to_know: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "path", name="uq_document_path"),
        CheckConstraint(
            "sensitivity IN " + str(SENSITIVITY_VALUES), name="ck_document_sensitivity"
        ),
        Index("ix_documents_tenant", "tenant_id"),
        Index("ix_documents_allowed_groups", "allowed_groups", postgresql_using="gin"),
        Index("ix_documents_content_hash", "content_hash"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    source: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    doc_type: Mapped[str] = mapped_column(Text, nullable=False, default="markdown")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- ABAC resource attributes ---
    sensitivity: Mapped[str] = mapped_column(Text, nullable=False, default="internal")
    owner_group: Mapped[str | None] = mapped_column(Text)
    allowed_groups: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    need_to_know_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    min_clearance: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    jurisdiction: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    acl_rule: Mapped[str | None] = mapped_column(Text)

    frontmatter: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ingested_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunk_ordinal"),
        CheckConstraint("sensitivity IN " + str(SENSITIVITY_VALUES), name="ck_chunk_sensitivity"),
        CheckConstraint("acl_source IN ('inherited', 'override')", name="ck_chunk_acl_source"),
        Index("ix_chunks_tenant", "tenant_id"),
        Index("ix_chunks_document", "document_id"),
        Index("ix_chunks_allowed_groups", "allowed_groups", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Denormalised effective ACL -- see module docstring.
    sensitivity: Mapped[str] = mapped_column(Text, nullable=False, default="internal")
    allowed_groups: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    min_clearance: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    need_to_know_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    jurisdiction: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    acl_source: Mapped[str] = mapped_column(Text, nullable=False, default="inherited")
    acl_rule: Mapped[str | None] = mapped_column(Text)

    # Injection classifier verdict, computed at ingest. Stored rather than recomputed so
    # that flags can be reviewed and triaged, not just reacted to inside one query.
    injection_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    injection_signals: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    # One column per embedding space. The dimension is part of the column type, so a
    # single nullable column cannot serve two models -- and Phase 3's ablation needs both
    # indexed at once to compare retrieval quality over the same corpus.
    #
    # These live on `chunks` rather than in a join table on purpose: the RLS policy is on
    # this table, so the authorization predicate and the ANN scan meet in one relation
    # instead of being separated by a join the planner would have to filter after.
    embedding_384: Mapped[list[float] | None] = mapped_column(HALFVEC(384))
    embedding_1536: Mapped[list[float] | None] = mapped_column(HALFVEC(1536))
    embedding_model: Mapped[str | None] = mapped_column(Text)

    # Generated by Postgres from `content`; never written by the application. Declared
    # here so queries can reference it, with FetchedValue so Alembic does not try to
    # manage a column the database owns.
    content_tsv: Mapped[str] = mapped_column(TSVECTOR, FetchedValue(), nullable=True)

    created_at: Mapped[datetime] = _now()


class Policy(Base):
    """Authorization rules stored as data, not code. The Phase 2 engine compiles these
    into SQL predicates; Phase 0 ships the table and a static baseline policy."""

    __tablename__ = "policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_policy_name"),
        CheckConstraint("effect IN ('allow', 'deny')", name="ck_policy_effect"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    effect: Mapped[str] = mapped_column(Text, nullable=False, default="allow")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    predicate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _now()


class AuditEntry(Base):
    """Append-only, hash-chained record of every authorization decision.

    ``prev_hash``/``entry_hash`` make the log tamper-evident: altering or deleting any
    row breaks the chain from that point forward.
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_tenant_time", "tenant_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    occurred_at: Mapped[datetime] = _now()

    action: Mapped[str] = mapped_column(Text, nullable=False)
    query_text: Mapped[str | None] = mapped_column(Text)
    retrieved_doc_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    denied_doc_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column()
    answer_hash: Mapped[str | None] = mapped_column(String(64))

    prev_hash: Mapped[str | None] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class CacheEpoch(Base):
    """Monotonic counter retiring the query cache when visibility changes.

    A row per bump, with a reason, rather than a single mutable value: knowing *why* the
    cache was retired is what lets you tell a policy change apart from a reindex when the
    hit rate collapses.
    """

    __tablename__ = "cache_epochs"

    epoch: Mapped[int] = mapped_column(Integer, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _now()


class QueryCacheEntry(Base):
    """One cached retrieval decision, scoped to an entitlement fingerprint.

    Stores chunk *ids*, never content: a hit re-fetches them through RLS, so the cache
    cannot become a copy of the corpus that lives outside the access model. See
    `gatekeeper.retrieval.cache` for the reasoning.
    """

    __tablename__ = "query_cache"
    __table_args__ = (
        Index("ix_query_cache_fingerprint", "fingerprint"),
        Index("ix_query_cache_epoch", "epoch"),
    )

    id: Mapped[uuid.UUID] = _pk()
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    embedding_384: Mapped[list[float] | None] = mapped_column(HALFVEC(384))
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _now()
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestJob(Base):
    """A unit of background ingestion work, with per-document failure isolation.

    The shape is a reaction to what actually went wrong during development: a single
    MinIO clock-skew error killed a 25-minute embedding run, and an unauthenticated
    Hugging Face fetch stalled another. Both were transient and both cost the whole batch.

    So a job is one row covering many documents, and a document that fails is recorded in
    `failures` while the rest of the batch continues. `gatekeeper jobs retry` re-enqueues
    only the failures — the dead-letter queue is a column, not a second system.
    """

    __tablename__ = "ingest_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed')",
            name="ck_ingest_job_status",
        ),
        Index("ix_ingest_jobs_status", "status", "enqueued_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # [{"path": ..., "error": ...}] -- the dead letters, kept next to the job that
    # produced them so a retry knows exactly what to re-run.
    failures: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text)

    enqueued_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def progress(self) -> float:
        return self.done / self.total if self.total else 0.0
