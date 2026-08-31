"""background ingestion jobs, with the dead-letter queue as a column

A job covers many documents and isolates their failures: `failures` records the ones that
did not make it while the rest of the batch proceeds. That shape is a direct reaction to
two real incidents during development — a MinIO clock-skew error and a stalled Hugging
Face fetch, both transient, each of which killed an entire 25-minute embedding run.

A separate dead-letter queue was considered and rejected. The failures belong to the job
that produced them; splitting them into another table or another Redis stream means a
retry has to reconstruct which job it is retrying, and the operator has to look in two
places to answer "did the reindex work".

Hand-written. `--autogenerate` in this repo drops every raw-SQL index; see 0010.

Revision ID: 0011_ingest_jobs
Revises: 0010_query_cache
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_ingest_jobs"
down_revision: str | None = "0010_query_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunks_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "enqueued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed')",
            name="ck_ingest_job_status",
        ),
    )
    op.create_index("ix_ingest_jobs_status", "ingest_jobs", ["status", "enqueued_at"])

    # Admin plane only: a job row carries no tenant data, and the query plane has no
    # business enqueuing work.
    op.execute("REVOKE ALL ON ingest_jobs FROM gatekeeper_app")


def downgrade() -> None:
    op.drop_index("ix_ingest_jobs_status", table_name="ingest_jobs")
    op.drop_table("ingest_jobs")
