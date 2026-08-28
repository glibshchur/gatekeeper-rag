"""embedding columns and HNSW indexes on chunks

Two notes that are easy to get wrong and expensive to discover later.

**Why halfvec.** ``halfvec`` stores 16-bit floats, halving index and heap size at a
recall cost that is negligible for normalised embeddings. For 15k chunks it is the
difference between a 45 MB and an 89 MB index; at Phase 5 scale it is the difference
between an index that fits in shared buffers and one that does not.

**Why cosine ops.** Every backend in ``gatekeeper.llm.embeddings`` returns L2-normalised
vectors, so cosine distance and inner product rank identically. Cosine is used anyway
because it stays correct if a future backend forgets to normalise.

The interaction between these indexes and row-level security is the interesting problem:
HNSW returns its ``ef_search`` candidates, RLS then removes the unauthorized ones, and
top-k comes back short. Phase 1 mitigates this at query time with
``hnsw.iterative_scan``; Phase 2 measures the recall cost properly and adds per-tenant
partial indexes.

Revision ID: 0003_embeddings
Revises: 0002_rls
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

revision: str = "0003_embeddings"
down_revision: str | None = "0002_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# dimension -> the backend that produces it
SPACES = {384: "bge-small-en-v1.5", 1536: "text-embedding-3-small"}


def upgrade() -> None:
    for dim in SPACES:
        op.add_column("chunks", sa.Column(f"embedding_{dim}", HALFVEC(dim), nullable=True))
    op.add_column("chunks", sa.Column("embedding_model", sa.Text(), nullable=True))

    for dim in SPACES:
        # Partial index: a chunk that has not been embedded in this space contributes
        # nothing to the graph, and excluding NULLs keeps the index proportional to what
        # is actually indexed rather than to the table.
        op.execute(
            f"CREATE INDEX ix_chunks_embedding_{dim}_hnsw ON chunks "
            f"USING hnsw (embedding_{dim} halfvec_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64) "
            f"WHERE embedding_{dim} IS NOT NULL"
        )


def downgrade() -> None:
    for dim in SPACES:
        op.execute(f"DROP INDEX IF EXISTS ix_chunks_embedding_{dim}_hnsw")
    op.drop_column("chunks", "embedding_model")
    for dim in SPACES:
        op.drop_column("chunks", f"embedding_{dim}")
