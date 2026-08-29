"""hoist the claims lookup out of the per-row predicate, properly this time

Migration 0006 tried to make the claim accessors run once per statement by wrapping each
one in a scalar subquery *inside* `gatekeeper.authorize()`. It made things three times
worse:

    per-row accessors (0005):                797 ms
    scalar subqueries inside authorize (0006):  2,612 ms

The reason is SQL function inlining. Postgres inlines a `LANGUAGE sql` function into the
calling query only when its body is a simple expression; a body containing subqueries is
not inlinable. So 0006 converted `authorize()` from an expression the planner could fold
into the query's quals into an opaque function called once per row — and each of those
calls then executed seven InitPlans of its own. The optimisation defeated the mechanism
it depended on.

The fix is to hoist at the *call site* instead. `gatekeeper.claims_row()` returns every
session-derived value as one composite, and the policies pass
`(SELECT gatekeeper.claims_row())` as an argument. That subquery lives in the outer
query, where it is uncorrelated and becomes a genuine InitPlan evaluated once per
statement. `authorize()` goes back to being a simple expression over its arguments — and
can now be `IMMUTABLE`, since it no longer reads session state at all, which makes it
maximally inlinable.

The wider point, worth more than the milliseconds: moving session state from *ambient*
(read inside the predicate) to *explicit* (passed in as an argument) is what made the
performance problem tractable. It also makes `authorize()` a pure function, which is why
it can be tested without a session.

Revision ID: 0007_claims_row
Revises: 0006_initplan
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_claims_row"
down_revision: str | None = "0006_initplan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_BODY = """
    gatekeeper.authorize(
        tenant_id, sensitivity, allowed_groups, min_clearance,
        need_to_know_tags, jurisdiction,
        (SELECT gatekeeper.claims_row())
    )
"""


def upgrade() -> None:
    op.execute("""
        CREATE TYPE gatekeeper.claims_t AS (
            tenant       uuid,
            clearance    smallint,
            expired      boolean,
            groups       text[],
            need_to_know text[],
            region       text,
            denied_tags  text[]
        );
    """)

    # Everything the access model reads from the session, gathered once. This is the only
    # function that touches `current_setting` or the `policies` table on a read path.
    op.execute("""
        CREATE OR REPLACE FUNCTION gatekeeper.claims_row() RETURNS gatekeeper.claims_t
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT
                gatekeeper.current_tenant(),
                gatekeeper.current_clearance(),
                gatekeeper.claims_expired(),
                gatekeeper.current_groups(),
                gatekeeper.current_need_to_know(),
                gatekeeper.current_region(),
                gatekeeper.denied_tags()
        $$;
    """)

    # IMMUTABLE, not STABLE: with the claims passed in, the result depends only on the
    # arguments. That is both true and useful -- it is the strongest hint for inlining.
    op.execute("""
        CREATE OR REPLACE FUNCTION gatekeeper.authorize(
            p_tenant        uuid,
            p_sensitivity   text,
            p_groups        text[],
            p_min_clearance smallint,
            p_tags          text[],
            p_jurisdiction  text[],
            c               gatekeeper.claims_t
        ) RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
            SELECT
                p_tenant = c.tenant
                AND NOT c.expired
                AND NOT (p_tags && c.denied_tags)
                AND p_min_clearance <= c.clearance
                AND (p_sensitivity = 'public' OR p_groups && c.groups)
                AND (cardinality(p_tags) = 0 OR p_tags <@ c.need_to_know)
                AND (
                    cardinality(p_jurisdiction) = 0
                    OR c.region = ANY(p_jurisdiction)
                    OR 'global' = ANY(c.need_to_know)
                )
        $$;
    """)

    op.execute("GRANT USAGE ON TYPE gatekeeper.claims_t TO gatekeeper_app")
    op.execute("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA gatekeeper TO gatekeeper_app")

    for table in ("documents", "chunks"):
        op.execute(f"DROP POLICY IF EXISTS {table}_read ON {table}")
        op.execute(f"CREATE POLICY {table}_read ON {table} FOR SELECT USING ({POLICY_BODY})")

    # The six-argument form is now unreferenced. Dropping it rather than leaving it is
    # deliberate: two versions of the access model in the same schema is exactly the
    # drift ADR 0005 exists to prevent.
    op.execute("""
        DROP FUNCTION IF EXISTS gatekeeper.authorize(
            uuid, text, text[], smallint, text[], text[]
        );
    """)


def downgrade() -> None:
    # The 0005-era per-row form, restated rather than imported: a migration that reaches
    # into a sibling revision's module breaks the moment that file is renamed.
    op.execute("""
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
    """)
    for table in ("documents", "chunks"):
        op.execute(f"DROP POLICY IF EXISTS {table}_read ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_read ON {table} FOR SELECT
            USING (gatekeeper.authorize(
                tenant_id, sensitivity, allowed_groups, min_clearance,
                need_to_know_tags, jurisdiction
            ));
        """)
    op.execute("""
        DROP FUNCTION IF EXISTS gatekeeper.authorize(
            uuid, text, text[], smallint, text[], text[], gatekeeper.claims_t
        );
    """)
    op.execute("DROP FUNCTION IF EXISTS gatekeeper.claims_row()")
    op.execute("DROP TYPE IF EXISTS gatekeeper.claims_t")
