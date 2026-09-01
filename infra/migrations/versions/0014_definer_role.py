"""give the SECURITY DEFINER functions a minimal owner

0012 and 0013 moved two privileged operations out of the API process and into
`SECURITY DEFINER` functions. That removed the credential, but left the functions running
as `postgres` — a superuser — because a function's definer context is its *owner*, and
migrations create objects owned by whoever runs them.

So the honest description of the state after 0013 was: narrow function bodies, maximal
authority behind them. Since the point of the exercise is least privilege, that is
half a fix.

`gatekeeper_definer` is `NOLOGIN` (nothing can authenticate as it) and `BYPASSRLS` with
`SELECT` on exactly the three tables the two functions read. `BYPASSRLS` is required
rather than incidental: `principals` and `chunks` are `FORCE`d, so a non-superuser owner
would otherwise be subject to the very policies these functions exist to bootstrap and
measure against.

What this buys, concretely: if a future function is added to this schema and gets
something wrong, it inherits SELECT on three tables instead of the ability to do anything
at all to the cluster.

Hand-written. `--autogenerate` in this repo drops every raw-SQL index; see 0010.

Revision ID: 0014_definer_role
Revises: 0013_resolve_principal
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_definer_role"
down_revision: str | None = "0013_resolve_principal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER = "gatekeeper_definer"
WITHHELD = "gatekeeper.withheld_summary(uuid, text, text, integer, integer, integer, uuid[])"
RESOLVE = "gatekeeper.resolve_principal(text, text)"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEFINER}') THEN
                -- NOLOGIN: this role exists to own functions, never to connect.
                CREATE ROLE {DEFINER} NOLOGIN BYPASSRLS;
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public, gatekeeper TO {DEFINER}")
    # Exactly what the two function bodies read, and nothing else. No INSERT, UPDATE or
    # DELETE anywhere: neither function writes.
    op.execute(f"GRANT SELECT ON public.chunks, public.principals, public.tenants TO {DEFINER}")

    op.execute(f"ALTER FUNCTION {WITHHELD} OWNER TO {DEFINER}")
    op.execute(f"ALTER FUNCTION {RESOLVE} OWNER TO {DEFINER}")

    # ALTER ... OWNER resets the ACL, so the grants from 0012/0013 are reapplied here.
    for signature in (WITHHELD, RESOLVE):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO gatekeeper_app")


def downgrade() -> None:
    op.execute(f"ALTER FUNCTION {WITHHELD} OWNER TO CURRENT_USER")
    op.execute(f"ALTER FUNCTION {RESOLVE} OWNER TO CURRENT_USER")
    for signature in (WITHHELD, RESOLVE):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO gatekeeper_app")
    # The role is deliberately **not dropped**, matching 0002, which creates
    # `gatekeeper_app` and likewise leaves it. A role is a *cluster* object while a
    # migration is scoped to one database, so dropping it here would reach outside this
    # migration's blast radius.
    #
    # That is not theoretical. `make migrate-check` builds a scratch database in the same
    # cluster; a `DROP ROLE` in this downgrade failed there with "7 objects in database
    # gatekeeper" — the *live* database's grants, which `DROP OWNED BY` cannot see because
    # it only covers the current one. Revoking this database's privileges is the whole of
    # what a per-database downgrade can correctly do.
    op.execute(f"REVOKE ALL ON public.chunks, public.principals, public.tenants FROM {DEFINER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public, gatekeeper FROM {DEFINER}")
