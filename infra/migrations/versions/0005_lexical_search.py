"""full-text search vector on chunks, for the lexical half of hybrid retrieval

Dense retrieval fails in a predictable way: it is good at meaning and bad at *tokens*.
A query for "SEC-4417", "IRS Form 1099", or "gitlab-org/gitlab#12345" has no useful
neighbourhood in embedding space, and a policy corpus is full of exactly those --
thresholds, form numbers, entity names, ticket IDs. Lexical search is good at precisely
what dense search is bad at, which is why fusing them beats either.

**This is `ts_rank_cd`, not BM25, and the difference is worth naming.** Postgres's
built-in ranking is a cover-density measure, not Okapi BM25: no document-length
normalisation of the same shape, no tunable k1/b. Getting real BM25 in Postgres means
ParadeDB's `pg_search`, which is a different base image and a heavier dependency.

The reason that is an acceptable trade here, rather than a corner cut: **Reciprocal Rank
Fusion consumes ranks, not scores.** Only the ordering the lexical scorer produces
survives into the fusion, so the scorer's calibration is irrelevant. It would matter a
great deal in a weighted linear combination of scores, which is one of several reasons
this project does not use one.

The column is `GENERATED ALWAYS AS ... STORED` rather than trigger-maintained: the
tsvector cannot drift from the content it indexes, because Postgres will not let it.

Revision ID: 0005_lexical
Revises: 0004_abac
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_lexical"
down_revision: str | None = "0004_abac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 'english' applies stemming and stop-word removal. That is right for prose and
    # slightly wrong for identifiers, which is a trade the corpus justifies: the handbook
    # is overwhelmingly prose, and `simple` would lose "expensing" -> "expense".
    op.execute("""
        ALTER TABLE chunks
        ADD COLUMN content_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
    """)
    op.execute("CREATE INDEX ix_chunks_content_tsv ON chunks USING gin (content_tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_content_tsv")
    op.drop_column("chunks", "content_tsv")
