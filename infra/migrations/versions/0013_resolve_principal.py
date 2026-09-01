"""authentication bootstrap as a function, not an owner connection

The last request-path caller of `admin_session()`, and the one that mattered most:
`seed.load_principal()`, invoked by `auth.resolve()` on *every* request to read the
caller's groups, clearance and need-to-know from the database. That is the mechanism
behind this project's central claim — a token asserts identity, the database owns
entitlement — and it was reading over a connection that bypasses row-level security.

It is a genuine chicken-and-egg, not carelessness. The `principals_read` policy is:

    tenant_id = gatekeeper.current_tenant()
    AND NOT gatekeeper.claims_expired()
    AND (id = gatekeeper.current_principal() OR clearance <= gatekeeper.current_clearance())

Every clause reads the claims GUC. Resolution is what *produces* the claims, so at that
moment the GUC is empty and no policy can match. No RLS policy can solve this; something
has to run with authority outside the policy.

`gatekeeper.resolve_principal()` is that something, scoped as narrowly as the job allows:
it takes an exact tenant slug and external id, and returns at most one row. It cannot
list, cannot pattern-match, and cannot enumerate — so it discloses exactly what
authenticating as that handle already discloses, and nothing about who else exists.

Found by pointing `GK_DATABASE_OWNER_URL` at a dead host and watching the API 500. Reading
the code had already missed it twice.

Hand-written. `--autogenerate` in this repo drops every raw-SQL index; see 0010. The
revision id is short because `alembic_version.version_num` is `varchar(32)`.

Revision ID: 0013_resolve_principal
Revises: 0012_owner_out_of_api
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_resolve_principal"
down_revision: str | None = "0012_owner_out_of_api"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "gatekeeper_app"

SIGNATURE = "gatekeeper.resolve_principal(text, text)"

RESOLVE_PRINCIPAL = """
CREATE OR REPLACE FUNCTION gatekeeper.resolve_principal(
    p_tenant_slug text,
    p_external_id text
)
RETURNS TABLE (
    id              uuid,
    tenant_id       uuid,
    external_id     text,
    email           text,
    display_name    text,
    groups          text[],
    clearance       smallint,
    department      text,
    region          text,
    employment_type text,
    need_to_know    text[],
    valid_until     timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
-- Mandatory on SECURITY DEFINER: without it the caller controls `search_path` and can
-- shadow `principals` with a table of their own, which then resolves owner-side.
SET search_path = pg_catalog, public
AS $fn$
    SELECT p.id, p.tenant_id, p.external_id, p.email, p.display_name, p.groups,
           p.clearance, p.department, p.region, p.employment_type, p.need_to_know,
           p.valid_until
    FROM public.principals p
    JOIN public.tenants t ON t.id = p.tenant_id
    -- Equality only. No LIKE, no regex, no NULL-matches-everything: this must resolve one
    -- known handle, never enumerate the directory.
    WHERE t.slug = p_tenant_slug
      AND p.external_id = p_external_id
    LIMIT 1;
$fn$;
"""


def upgrade() -> None:
    op.execute(RESOLVE_PRINCIPAL)
    # Postgres grants EXECUTE to PUBLIC on a new function by default, which on a
    # SECURITY DEFINER function hands the owner's privileges to every role in the cluster.
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
