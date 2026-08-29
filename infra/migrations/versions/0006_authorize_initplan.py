"""evaluate the claim accessors once per statement instead of once per row

ADR 0005 claimed that collapsing deny rules into a single tag set via
`gatekeeper.denied_tags()` made data-driven deny affordable, because the policy lookup
would happen "once per statement". That was wrong, and the lexical retrieval work in
Phase 3 is what exposed it:

    lexical query, 8,239 matching rows, as the table owner (no RLS):   70 ms
    the same query as a principal (RLS active):                       797 ms

An 11x cost, on a query touching only 8,000 rows. `STABLE` promises the planner that a
function will not change *within a statement*; it does not promise that the function will
be *called* only once. A no-argument `STABLE` function in a `WHERE` clause is re-evaluated
per row, so every candidate row was re-parsing the claims JSON six times and re-querying
the `policies` table once.

The fix is the standard Postgres idiom for this: wrap each no-argument call in an
uncorrelated scalar subquery. `(SELECT gatekeeper.denied_tags())` depends on nothing in
the outer query, so the planner hoists it into an InitPlan and executes it exactly once
per statement. The function bodies are unchanged; only how `authorize()` calls them
changes.

This matters far more for the lexical path than the dense one. Dense retrieval reaches
the predicate with a few hundred candidate rows from the HNSW graph; lexical retrieval
reaches it with every row matching the tsquery. Same predicate, two orders of magnitude
difference in how often it runs.

Revision ID: 0006_initplan
Revises: 0005_lexical
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_initplan"
down_revision: str | None = "0005_lexical"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every accessor is wrapped. The claims JSON parse is cheap individually and was
# happening six times per candidate row; denied_tags() was a table query per row.
AUTHORIZE_INITPLAN = """
CREATE OR REPLACE FUNCTION gatekeeper.authorize(
    p_tenant        uuid,
    p_sensitivity   text,
    p_groups        text[],
    p_min_clearance smallint,
    p_tags          text[],
    p_jurisdiction  text[]
) RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT
        p_tenant = (SELECT gatekeeper.current_tenant())
        AND NOT (SELECT gatekeeper.claims_expired())
        AND NOT (p_tags && (SELECT gatekeeper.denied_tags()))
        AND p_min_clearance <= (SELECT gatekeeper.current_clearance())
        AND (
            p_sensitivity = 'public'
            OR p_groups && (SELECT gatekeeper.current_groups())
        )
        AND (
            cardinality(p_tags) = 0
            OR p_tags <@ (SELECT gatekeeper.current_need_to_know())
        )
        AND (
            cardinality(p_jurisdiction) = 0
            OR (SELECT gatekeeper.current_region()) = ANY(p_jurisdiction)
            -- The whole comparison is wrapped, not just the accessor: Postgres parses
            -- `ANY((SELECT ...))` as the subquery form of ANY, which compares a scalar
            -- against rows rather than against an array's elements, and fails with
            -- "malformed array literal". Wrapping the boolean keeps it uncorrelated and
            -- therefore still an InitPlan.
            OR (SELECT 'global' = ANY(gatekeeper.current_need_to_know()))
        )
$$;
"""

AUTHORIZE_PER_ROW = """
CREATE OR REPLACE FUNCTION gatekeeper.authorize(
    p_tenant        uuid,
    p_sensitivity   text,
    p_groups        text[],
    p_min_clearance smallint,
    p_tags          text[],
    p_jurisdiction  text[]
) RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT
        p_tenant = gatekeeper.current_tenant()
        AND NOT gatekeeper.claims_expired()
        AND NOT (p_tags && gatekeeper.denied_tags())
        AND p_min_clearance <= gatekeeper.current_clearance()
        AND (
            p_sensitivity = 'public'
            OR p_groups && gatekeeper.current_groups()
        )
        AND (
            cardinality(p_tags) = 0
            OR p_tags <@ gatekeeper.current_need_to_know()
        )
        AND (
            cardinality(p_jurisdiction) = 0
            OR gatekeeper.current_region() = ANY(p_jurisdiction)
            OR 'global' = ANY(gatekeeper.current_need_to_know())
        )
$$;
"""


def upgrade() -> None:
    op.execute(AUTHORIZE_INITPLAN)


def downgrade() -> None:
    op.execute(AUTHORIZE_PER_ROW)
