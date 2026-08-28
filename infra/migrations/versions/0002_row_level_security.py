"""row-level security, claim helpers, and the unprivileged application role

This migration is the security substrate of the whole system. Three things happen here,
and all three matter:

1. **Claim helpers.** ``gatekeeper.claims()`` reads a transaction-local GUC that the
   application sets from the authenticated Principal. Policies call the helpers rather
   than parsing JSON inline, so the authorization logic reads like the access model.

2. **Policies, with FORCE.** ``ENABLE ROW LEVEL SECURITY`` alone is not enough: the table
   owner is exempt unless ``FORCE`` is also set. Without ``FORCE``, every claim this
   project makes about database-enforced authorization would be false.

3. **A role that cannot bypass RLS.** ``gatekeeper_app`` is created ``NOSUPERUSER
   NOBYPASSRLS`` and granted only DML. The query plane connects as this role, so a
   compromised API process still cannot read rows it is not entitled to. The admin plane
   (migrations, ingestion) keeps the owner connection.

The Phase 0 predicate is deliberately close to the naive model it replaces -- group
overlap plus a clearance floor -- so that the Phase 2 ABAC engine can be measured against
a real baseline rather than against nothing. Region and need-to-know are stored and
indexed here but not yet enforced.

Revision ID: 0002_rls
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_rls"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "gatekeeper_app"
GUC = "gatekeeper.principal"

# Tables that carry tenant data and are therefore subject to RLS.
PROTECTED = ("tenants", "principals", "documents", "chunks", "policies", "audit_log")


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS gatekeeper")

    # --- claim accessors --------------------------------------------------
    # STABLE (not IMMUTABLE): the value is fixed within a statement but varies across
    # transactions. Marking these IMMUTABLE would let the planner cache them across
    # principals, which is a correctness bug disguised as an optimisation.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION gatekeeper.claims() RETURNS jsonb
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT coalesce(nullif(current_setting('{GUC}', true), ''), '{{}}')::jsonb
        $$;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION gatekeeper.current_tenant() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT nullif(gatekeeper.claims() ->> 'tenant', '')::uuid
        $$;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION gatekeeper.current_principal() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT nullif(gatekeeper.claims() ->> 'principal', '')::uuid
        $$;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION gatekeeper.current_groups() RETURNS text[]
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT ARRAY(SELECT jsonb_array_elements_text(gatekeeper.claims() -> 'groups'))
        $$;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION gatekeeper.current_clearance() RETURNS smallint
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT coalesce((gatekeeper.claims() ->> 'clearance')::smallint, 0::smallint)
        $$;
    """)

    # --- the application role --------------------------------------------
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_ROLE}'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
            END IF;
        END
        $$;
    """)
    op.execute(f"GRANT CONNECT ON DATABASE {op.get_bind().engine.url.database} TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public, gatekeeper TO {APP_ROLE}")
    op.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA gatekeeper TO {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )

    # --- enable + force ----------------------------------------------------
    for table in PROTECTED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # --- policies ----------------------------------------------------------
    # Every policy is tenant-scoped first. An unset GUC yields NULL, and `x = NULL` is
    # false, so a session that forgets to set claims sees nothing rather than everything.
    op.execute("""
        CREATE POLICY tenants_read ON tenants FOR SELECT
        USING (id = gatekeeper.current_tenant());
    """)

    # A principal may read their own row and the roster of their tenant, but clearance
    # gates visibility of people above them.
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

    op.execute("""
        CREATE POLICY documents_read ON documents FOR SELECT
        USING (
            tenant_id = gatekeeper.current_tenant()
            AND min_clearance <= gatekeeper.current_clearance()
            AND (
                sensitivity = 'public'
                OR allowed_groups && gatekeeper.current_groups()
            )
        );
    """)

    # Chunks carry a denormalised effective ACL so this predicate can be pushed into the
    # vector scan in Phase 1 without a join to documents.
    op.execute("""
        CREATE POLICY chunks_read ON chunks FOR SELECT
        USING (
            tenant_id = gatekeeper.current_tenant()
            AND min_clearance <= gatekeeper.current_clearance()
            AND (
                sensitivity = 'public'
                OR allowed_groups && gatekeeper.current_groups()
            )
        );
    """)

    op.execute("""
        CREATE POLICY policies_read ON policies FOR SELECT
        USING (tenant_id = gatekeeper.current_tenant());
    """)

    # The audit log is append-only from the data plane: insert your own entries, read
    # your tenant's, never update or delete. Deletion breaks the hash chain by design.
    op.execute("""
        CREATE POLICY audit_read ON audit_log FOR SELECT
        USING (tenant_id = gatekeeper.current_tenant());
    """)
    op.execute("""
        CREATE POLICY audit_append ON audit_log FOR INSERT
        WITH CHECK (
            tenant_id = gatekeeper.current_tenant()
            AND principal_id IS NOT DISTINCT FROM gatekeeper.current_principal()
        );
    """)


def downgrade() -> None:
    for table, policies in {
        "audit_log": ("audit_read", "audit_append"),
        "policies": ("policies_read",),
        "chunks": ("chunks_read",),
        "documents": ("documents_read",),
        "principals": ("principals_read",),
        "tenants": ("tenants_read",),
    }.items():
        for policy in policies:
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public, gatekeeper FROM {APP_ROLE}")
    op.execute("DROP SCHEMA IF EXISTS gatekeeper CASCADE")
