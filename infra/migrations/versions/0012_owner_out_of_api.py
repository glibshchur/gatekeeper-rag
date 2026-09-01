"""take the RLS-bypassing credential out of the API process

`docs/THREAT_MODEL.md` adversary A3. The split-privilege story is that the query layer
connects as `gatekeeper_app` — `NOSUPERUSER`, `NOBYPASSRLS` — so it *cannot* return a row
the policy forbids. That was true of the retrieval path and false of the process running
it: three parts of the request path opened a second connection as the table owner, which
bypasses row-level security entirely.

Each had a reason. None of them needed the owner.

**1. The withheld count.** To report "3 results withheld by authorization" the system must
run the same search *unfiltered* and diff the two. An authorized baseline would remove
exactly the rows it is trying to count, reporting 0 withheld while withholding plenty —
worse than not reporting at all. So the baseline genuinely needs to see denied rows.

It does not need the *caller* to see them. `gatekeeper.withheld_summary()` is
`SECURITY DEFINER`: the privilege lives in the database, executes owner-side, and returns
a count plus the denied document ids — never chunk content, never chunk ids. The privilege
stops being a credential sitting in an environment variable and becomes a function with a
fixed, narrow output.

**2. The query cache.** 0010 revoked all access from `gatekeeper_app` on the reasoning
that the cache is admin-plane. The effect was the opposite of the intent: rather than
keeping the data plane away from the cache, it dragged the *owner credential* into the
data plane. The cache holds entitlement hashes and chunk ids and no content, so the app
role gets exactly what the hot path needs — SELECT, INSERT, UPDATE — and nothing else.
DELETE stays owner-only, because purging is a maintenance operation.

**3. `/api/jobs`.** Read-only, so SELECT. Writing and enqueuing stay owner-only: the query
plane still has no business creating work.

After this, `GK_DATABASE_OWNER_URL` is unused by the API process. A code-execution
compromise there is bounded by the same policy as everything else, which is what the
design claimed all along.

Hand-written. `--autogenerate` in this repo drops every raw-SQL index; see 0010. The
revision id is kept short because `alembic_version.version_num` is `varchar(32)` and a
longer one fails *after* the migration body has run.

Revision ID: 0012_owner_out_of_api
Revises: 0011_ingest_jobs
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_owner_out_of_api"
down_revision: str | None = "0011_ingest_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "gatekeeper_app"

# Ordering matters and is the whole subtlety. The count must be
# "top-k over everything, minus what you saw" — not "top-k over what you did not see".
# Filtering before LIMIT changes the answer: when every one of the top k is visible the
# first form correctly reports 0 withheld, and the second happily returns the next k rows
# and reports k. The CTE keeps the LIMIT on the unfiltered ranking.
WITHHELD_SUMMARY = """
CREATE OR REPLACE FUNCTION gatekeeper.withheld_summary(
    p_tenant    uuid,
    p_query     text,
    p_model     text,
    p_dim       integer,
    p_k         integer,
    p_ef_search integer,
    p_visible   uuid[]
)
RETURNS TABLE (withheld integer, denied_documents uuid[])
LANGUAGE plpgsql
SECURITY DEFINER
-- Mandatory on SECURITY DEFINER. Without it the caller controls `search_path` and can
-- shadow `chunks` with a table of their own, and the function resolves it owner-side.
SET search_path = pg_catalog, public
AS $fn$
DECLARE
    seen uuid[] := coalesce(p_visible, ARRAY[]::uuid[]);
BEGIN
    IF p_k IS NULL OR p_k < 1 OR p_k > 1000 THEN
        RAISE EXCEPTION 'withheld_summary: k out of range (%)', p_k;
    END IF;

    PERFORM set_config('hnsw.ef_search',
                       greatest(1, least(coalesce(p_ef_search, 200), 1000))::text, true);
    PERFORM set_config('hnsw.iterative_scan', 'relaxed_order', true);

    IF p_dim = 384 THEN
        RETURN QUERY
        WITH topk AS (
            SELECT c.id, c.document_id
            FROM public.chunks c
            WHERE c.tenant_id = p_tenant
              AND c.embedding_model = p_model
              AND c.embedding_384 IS NOT NULL
            ORDER BY c.embedding_384 OPERATOR(public.<=>) p_query::public.halfvec(384)
            LIMIT p_k
        )
        SELECT count(*)::integer,
               coalesce(array_agg(DISTINCT t.document_id), ARRAY[]::uuid[])
        FROM topk t
        WHERE NOT (t.id = ANY(seen));
    ELSIF p_dim = 1536 THEN
        RETURN QUERY
        WITH topk AS (
            SELECT c.id, c.document_id
            FROM public.chunks c
            WHERE c.tenant_id = p_tenant
              AND c.embedding_model = p_model
              AND c.embedding_1536 IS NOT NULL
            ORDER BY c.embedding_1536 OPERATOR(public.<=>) p_query::public.halfvec(1536)
            LIMIT p_k
        )
        SELECT count(*)::integer,
               coalesce(array_agg(DISTINCT t.document_id), ARRAY[]::uuid[])
        FROM topk t
        WHERE NOT (t.id = ANY(seen));
    ELSE
        RAISE EXCEPTION 'withheld_summary: unsupported embedding dimension (%)', p_dim;
    END IF;
END;
$fn$;
"""


def upgrade() -> None:
    op.execute(WITHHELD_SUMMARY)

    # Postgres grants EXECUTE to PUBLIC on a new function by default, which on a
    # SECURITY DEFINER function hands the owner's privileges to every role in the cluster.
    op.execute(
        "REVOKE ALL ON FUNCTION gatekeeper.withheld_summary("
        "uuid, text, text, integer, integer, integer, uuid[]) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION gatekeeper.withheld_summary("
        f"uuid, text, text, integer, integer, integer, uuid[]) TO {APP_ROLE}"
    )

    # The cache: exactly what the hot path does, and nothing else. No DELETE — purging
    # retired epochs is maintenance, and stays on the owner connection.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON query_cache TO {APP_ROLE}")
    # SELECT only. `bump_epoch` retires the whole cache and is an admin operation.
    op.execute(f"GRANT SELECT ON cache_epochs TO {APP_ROLE}")

    # Read-only. Enqueuing and progress writes stay owner-side, in the worker.
    op.execute(f"GRANT SELECT ON ingest_jobs TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON ingest_jobs FROM {APP_ROLE}")
    op.execute(f"REVOKE SELECT ON cache_epochs FROM {APP_ROLE}")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE ON query_cache FROM {APP_ROLE}")
    op.execute(
        "DROP FUNCTION IF EXISTS gatekeeper.withheld_summary("
        "uuid, text, text, integer, integer, integer, uuid[])"
    )
