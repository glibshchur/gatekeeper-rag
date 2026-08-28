"""the ABAC decision function, need-to-know, jurisdiction, and data-driven deny rules

Phase 0 deliberately shipped a predicate close to the naive model it replaced -- group
overlap plus a clearance floor -- so the real engine would have a baseline to be measured
against. This is that engine. It closes the gap ADR 0003 admitted: ``region``,
``need_to_know_tags`` and ``valid_until`` were stored and populated but never checked.

Four rules are added, and one design decision is worth stating because it is the whole
reason this is ABAC and not RBAC with extra columns:

* **Need-to-know is subset, not overlap.** A resource tagged ``{pii, compensation}``
  requires the principal to hold *both*. Overlap would mean any one tag unlocks the
  document, which is the opposite of need-to-know.
* **Jurisdiction scopes, it does not gate.** A document carrying ``jurisdiction`` is
  visible to principals in that region, or to anyone holding the ``global`` grant. This
  is a policy choice, not a law: plenty of companies let everyone read every country's
  handbook. It is modelled here because the corpus genuinely partitions employment
  policy by legal entity and that is the clearest available demonstration of an
  attribute that is not a group.
* **Expiry is re-checked in the database.** ``Principal.to_claims()`` already refuses to
  serialise an expired grant. That is not enough: an application that caches claims, or
  reuses a Principal object built minutes earlier, would outlive the grant. The claims
  carry ``exp`` and the engine checks it.
* **Deny overrides allow.** Deny rules live in the ``policies`` table as data. They are
  collapsed into a single tag set per statement by ``gatekeeper.denied_tags()`` rather
  than evaluated per row -- a correlated subquery inside an RLS predicate would run once
  per candidate row, which on a 74k-chunk vector scan is not survivable.

Revision ID: 0004_abac
Revises: 0003_embeddings
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_abac"
down_revision: str | None = "0003_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEXT_ARRAY = postgresql.ARRAY(sa.Text())


def upgrade() -> None:
    # --- new attribute columns ------------------------------------------------
    op.add_column(
        "principals",
        sa.Column("need_to_know", TEXT_ARRAY, nullable=False, server_default="{}"),
    )
    op.add_column(
        "chunks",
        sa.Column("need_to_know_tags", TEXT_ARRAY, nullable=False, server_default="{}"),
    )
    op.add_column(
        "chunks", sa.Column("jurisdiction", TEXT_ARRAY, nullable=False, server_default="{}")
    )
    op.add_column("chunks", sa.Column("acl_rule", sa.Text(), nullable=True))
    op.create_index(
        "ix_chunks_need_to_know", "chunks", ["need_to_know_tags"], postgresql_using="gin"
    )

    # Backfill chunks from their documents so the new predicate does not hide everything
    # the moment it is installed.
    op.execute("""
        UPDATE chunks c
        SET need_to_know_tags = d.need_to_know_tags,
            jurisdiction      = d.jurisdiction,
            acl_rule          = d.acl_rule
        FROM documents d
        WHERE d.id = c.document_id
    """)

    # --- claim accessors ------------------------------------------------------
    for name, expr, returns in (
        ("current_region", "gatekeeper.claims() ->> 'region'", "text"),
        ("current_employment_type", "gatekeeper.claims() ->> 'employment_type'", "text"),
    ):
        op.execute(f"""
            CREATE OR REPLACE FUNCTION gatekeeper.{name}() RETURNS {returns}
            LANGUAGE sql STABLE PARALLEL SAFE AS $$ SELECT nullif({expr}, '') $$;
        """)

    op.execute("""
        CREATE OR REPLACE FUNCTION gatekeeper.current_need_to_know() RETURNS text[]
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT ARRAY(SELECT jsonb_array_elements_text(gatekeeper.claims() -> 'need_to_know'))
        $$;
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION gatekeeper.claims_expired() RETURNS boolean
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT coalesce(
                (gatekeeper.claims() ->> 'exp')::timestamptz <= now(),
                false  -- absent exp means a standing grant, not an expired one
            )
        $$;
    """)

    # --- deny rules, collapsed once per statement -----------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION gatekeeper.denied_tags() RETURNS text[]
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT coalesce(array_agg(DISTINCT tag), ARRAY[]::text[])
            FROM policies p
            CROSS JOIN LATERAL jsonb_array_elements_text(
                coalesce(p.predicate -> 'resource_tags_any', '[]'::jsonb)
            ) AS tag
            WHERE p.enabled
              AND p.effect = 'deny'
              AND p.tenant_id = gatekeeper.current_tenant()
              AND NOT (
                  ARRAY(SELECT jsonb_array_elements_text(
                      coalesce(p.predicate -> 'unless_groups_any', '[]'::jsonb)))
                  && gatekeeper.current_groups()
              )
              AND (
                  p.predicate -> 'employment_type_in' IS NULL
                  OR gatekeeper.current_employment_type() IN (
                      SELECT jsonb_array_elements_text(p.predicate -> 'employment_type_in')
                  )
              )
        $$;
    """)

    # --- the decision function ------------------------------------------------
    # One function, called by every policy, so the access model is written down once and
    # `documents` and `chunks` cannot drift apart.
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
                -- Tenant first. An unset GUC yields NULL and `x = NULL` is false, so a
                -- session that forgets to set claims sees nothing rather than everything.
                p_tenant = gatekeeper.current_tenant()
                AND NOT gatekeeper.claims_expired()

                -- Deny overrides allow, and is checked before any grant is considered.
                AND NOT (p_tags && gatekeeper.denied_tags())

                -- Clearance is a ceiling, not a key: necessary, never sufficient.
                AND p_min_clearance <= gatekeeper.current_clearance()

                -- Group membership, waived only for explicitly public material.
                AND (
                    p_sensitivity = 'public'
                    OR p_groups && gatekeeper.current_groups()
                )

                -- Need-to-know: subset, not overlap. Every tag must be held.
                AND (
                    cardinality(p_tags) = 0
                    OR p_tags <@ gatekeeper.current_need_to_know()
                )

                -- Jurisdiction: your own region, or a global grant.
                AND (
                    cardinality(p_jurisdiction) = 0
                    OR gatekeeper.current_region() = ANY(p_jurisdiction)
                    OR 'global' = ANY(gatekeeper.current_need_to_know())
                )
        $$;
    """)

    op.execute("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA gatekeeper TO gatekeeper_app")

    # --- swap the policies over ----------------------------------------------
    op.execute("DROP POLICY IF EXISTS documents_read ON documents")
    op.execute("""
        CREATE POLICY documents_read ON documents FOR SELECT
        USING (gatekeeper.authorize(
            tenant_id, sensitivity, allowed_groups, min_clearance,
            need_to_know_tags, jurisdiction
        ));
    """)

    op.execute("DROP POLICY IF EXISTS chunks_read ON chunks")
    op.execute("""
        CREATE POLICY chunks_read ON chunks FOR SELECT
        USING (gatekeeper.authorize(
            tenant_id, sensitivity, allowed_groups, min_clearance,
            need_to_know_tags, jurisdiction
        ));
    """)

    # Expiry now gates the roster too; a lapsed contractor should not enumerate staff.
    op.execute("DROP POLICY IF EXISTS principals_read ON principals")
    op.execute("""
        CREATE POLICY principals_read ON principals FOR SELECT
        USING (
            tenant_id = gatekeeper.current_tenant()
            AND NOT gatekeeper.claims_expired()
            AND (
                id = gatekeeper.current_principal()
                OR clearance <= gatekeeper.current_clearance()
            )
        );
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS documents_read ON documents")
    op.execute("""
        CREATE POLICY documents_read ON documents FOR SELECT
        USING (
            tenant_id = gatekeeper.current_tenant()
            AND min_clearance <= gatekeeper.current_clearance()
            AND (sensitivity = 'public' OR allowed_groups && gatekeeper.current_groups())
        );
    """)
    op.execute("DROP POLICY IF EXISTS chunks_read ON chunks")
    op.execute("""
        CREATE POLICY chunks_read ON chunks FOR SELECT
        USING (
            tenant_id = gatekeeper.current_tenant()
            AND min_clearance <= gatekeeper.current_clearance()
            AND (sensitivity = 'public' OR allowed_groups && gatekeeper.current_groups())
        );
    """)
    op.execute("DROP POLICY IF EXISTS principals_read ON principals")
    op.execute("""
        CREATE POLICY principals_read ON principals FOR SELECT
        USING (
            tenant_id = gatekeeper.current_tenant()
            AND (
                id = gatekeeper.current_principal()
                OR clearance <= gatekeeper.current_clearance()
            )
        );
    """)

    for name in (
        "authorize(uuid, text, text[], smallint, text[], text[])",
        "denied_tags()",
        "claims_expired()",
        "current_need_to_know()",
        "current_employment_type()",
        "current_region()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS gatekeeper.{name}")

    op.drop_index("ix_chunks_need_to_know", table_name="chunks")
    op.drop_column("chunks", "acl_rule")
    op.drop_column("chunks", "jurisdiction")
    op.drop_column("chunks", "need_to_know_tags")
    op.drop_column("principals", "need_to_know")
