"""record the injection classifier's verdict on every chunk

Scored at ingest and stored, rather than computed at query time, for two reasons. The
rules are cheap but not free over a 20-chunk candidate set on every query, and — more
importantly — a stored verdict is reviewable. An operator can list what the classifier
flagged and act on it; a verdict recomputed per query exists only for the duration of one
answer and can never be triaged.

`injection_signals` carries the rule names rather than only a number. The score decides
thresholds; the signals are what a human actually reads when deciding whether a flag is
real, and on this corpus most of them are not — 164 of 73,801 chunks flag, largely on
prose about maintenance mode, on-call paging ("do not acknowledge"), and a security
handbook that discusses these very attacks.

Revision ID: 0009_injection
Revises: 0008_partial_hnsw
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_injection"
down_revision: str | None = "0008_partial_hnsw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column("injection_score", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "chunks",
        sa.Column(
            "injection_signals",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    # Partial: the flagged set is ~0.2% of the corpus, so an index over all of it would be
    # almost entirely rows nobody will ever query for.
    op.execute(
        "CREATE INDEX ix_chunks_injection_flagged ON chunks (injection_score DESC) "
        "WHERE injection_score >= 0.35"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_injection_flagged")
    op.drop_column("chunks", "injection_signals")
    op.drop_column("chunks", "injection_score")
