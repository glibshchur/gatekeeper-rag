"""partial HNSW indexes for the low-selectivity tiers

The other half of the ADR 0006 fix. `retrieval.search.coarse_predicate()` puts two of the
policy's own clauses back into the query so the planner can estimate them; this gives the
planner indexes those clauses can actually match.

**Why partition on `sensitivity` and not on tenant or group.** ADR 0006 proposed
per-tenant partial indexes. That is the wrong axis for this corpus and, on reflection, for
most single-tenant-heavy deployments: the cliff is *intra*-tenant. `guest` and `mira` are
in the same tenant and differ by a factor of 18 in what they can read, so a per-tenant
index helps neither. Group membership is the natural axis but has unbounded cardinality —
an index per group does not scale and choosing which groups deserve one needs statistics
this system does not collect.

`sensitivity` is a four-value column, it appears in the policy verbatim, and it correlates
strongly with selectivity: the most restricted principals are exactly the ones limited to
`public`. Two indexes cover the tiers that suffer.

A partial index is only usable when the query's predicate *implies* the index predicate,
which is precisely what `coarse_predicate()` supplies — a groupless principal's query
carries `sensitivity = 'public'` and can therefore use the public-only graph, ~5% the size
of the full one.

Revision ID: 0008_partial_hnsw
Revises: 0007_claims_row
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_partial_hnsw"
down_revision: str | None = "0007_claims_row"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name suffix, predicate) — one HNSW graph per coarse tier.
#
# `public` serves unauthenticated and external principals, who are both the most
# restricted and the most likely to be on a shared endpoint where latency is visible.
#
# `public_or_internal` serves the broad-employee tier. It exists because the step from
# "everything" to "the two lowest sensitivity levels" is where most of the corpus lives,
# so a principal with no confidential access still gets a graph rather than a scan.
TIERS = (
    ("public", "sensitivity = 'public'"),
    ("internal", "sensitivity IN ('public', 'internal')"),
)


def upgrade() -> None:
    for suffix, predicate in TIERS:
        op.execute(
            f"CREATE INDEX ix_chunks_emb384_{suffix}_hnsw ON chunks "
            f"USING hnsw (embedding_384 halfvec_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64) "
            f"WHERE embedding_384 IS NOT NULL AND {predicate}"
        )


def downgrade() -> None:
    for suffix, _ in TIERS:
        op.execute(f"DROP INDEX IF EXISTS ix_chunks_emb384_{suffix}_hnsw")
