"""The request path must work with no owner credential available at all.

`docs/THREAT_MODEL.md` A3. The claim is narrow and checkable: nothing reachable from a
request opens an RLS-bypassing connection. Asserting that by reading the code is how it
silently regressed in the first place — `admin_session()`'s docstring said "ingestion and
migrations only" while three request-path callers used it.

So the owner URL is pointed at a database that does not exist. Anything that reaches for
it fails loudly; a passing test means nothing reached for it. `test_the_fixture_actually_
bites` guards the guard.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import delete
from sqlalchemy import text as sa_text

from gatekeeper.config import get_settings
from gatekeeper.core.db import admin_session, dispose_engines, principal_session
from gatekeeper.core.models import Chunk
from gatekeeper.retrieval.search import _ann_query, withheld_summary

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.integration.conftest import Fixture

pytestmark = pytest.mark.integration

UNREACHABLE = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/does-not-exist"

# A unit vector. Every seeded chunk gets the same one, so all of them tie on distance and
# a top-k with k >= their count returns all of them — the ranking is not what is under
# test here, the visibility arithmetic is.
VECTOR = [1.0] + [0.0] * 383


class _StubEmbedder:
    """Just the three attributes the query path reads. A real ONNX session would add ~18
    seconds per module to measure nothing about authorization."""

    class space:  # noqa: N801
        model = "bge-small-en-v1.5"
        column = "embedding_384"
        dim = 384
        max_tokens = 512

    def encode_query(self, text: str) -> Any:
        import numpy as np

        return np.array(VECTOR, dtype="float32")


@pytest.fixture
async def seeded(fx: Fixture) -> AsyncIterator[dict[str, uuid.UUID]]:
    """One embedded chunk per tenant-A document, so the counts below are exact.

    Tenant A holds `public`, `eng`, `audit` and `board`. The engineer is in
    {all, engineering} at EMPLOYEE clearance, so exactly two of the four are readable —
    which makes "withheld" a number this test can assert rather than merely observe.
    """
    # name -> (sensitivity, allowed_groups, min_clearance). Mirrors the document ACLs the
    # `fx` fixture sets, so chunk and document agree and the policy has one answer.
    acls = {
        "public": ("public", [], 0),
        "eng": ("internal", ["engineering"], 1),
        "audit": ("confidential", ["audit"], 1),
        "board": ("restricted", ["audit"], 3),
    }
    async with admin_session() as session:
        for name, (sensitivity, groups, clearance) in acls.items():
            chunk = Chunk(
                tenant_id=fx.tenant_a,
                document_id=fx.doc_ids[name],
                ordinal=0,
                content=f"content of {name}",
                heading_path=[name],
                token_count=3,
                sensitivity=sensitivity,
                allowed_groups=groups,
                min_clearance=clearance,
                embedding_model="bge-small-en-v1.5",
            )
            chunk.embedding_384 = VECTOR
            session.add(chunk)
    yield fx.doc_ids
    async with admin_session() as session:
        await session.execute(delete(Chunk).where(Chunk.tenant_id == fx.tenant_a))


@pytest.fixture
async def no_owner_credential(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Simulate an API process that was never given `GK_DATABASE_OWNER_URL`."""
    await dispose_engines()
    monkeypatch.setattr(get_settings(), "database_owner_url", UNREACHABLE, raising=False)
    yield
    await dispose_engines()


async def test_the_fixture_actually_bites(no_owner_credential: None) -> None:
    """Guard on the guard. If pointing the owner URL at a dead host did not break
    `admin_session`, every other test in this file would pass without proving anything."""
    with pytest.raises(Exception) as exc:
        async with admin_session() as session:
            await session.execute(sa_text("SELECT 1"))
    assert "does-not-exist" in str(exc.value) or "Connect" in type(exc.value).__name__


async def test_the_engineer_sees_two_of_four_and_the_count_says_so(
    fx: Fixture, seeded: dict[str, uuid.UUID]
) -> None:
    """Non-vacuous: an exact count, and the exact documents behind it."""
    embedder = _StubEmbedder()
    async with principal_session(fx.engineer) as session:
        chunks = await _ann_query(
            session,
            embedder=embedder,  # type: ignore[arg-type]
            query_vector=VECTOR,
            k=4,
            ef_search=200,
            tenant_id=fx.tenant_a,
            principal=fx.engineer,
        )
        withheld, denied = await withheld_summary(
            session,
            embedder=embedder,  # type: ignore[arg-type]
            query_vector=VECTOR,
            k=4,
            ef_search=200,
            tenant_id=fx.tenant_a,
            visible_chunk_ids=[c.chunk_id for c in chunks],
        )

    assert len(chunks) == 2, "engineer reads public + eng only"
    assert withheld == 2
    assert denied == sorted([seeded["audit"], seeded["board"]])


async def test_that_count_is_produced_without_any_owner_connection(
    fx: Fixture, seeded: dict[str, uuid.UUID], no_owner_credential: None
) -> None:
    """The same assertion, with the owner credential pointed at a dead host. This is the
    A3 claim: the request path needs no RLS-bypassing connection."""
    embedder = _StubEmbedder()
    async with principal_session(fx.engineer) as session:
        chunks = await _ann_query(
            session,
            embedder=embedder,  # type: ignore[arg-type]
            query_vector=VECTOR,
            k=4,
            ef_search=200,
            tenant_id=fx.tenant_a,
            principal=fx.engineer,
        )
        withheld, denied = await withheld_summary(
            session,
            embedder=embedder,  # type: ignore[arg-type]
            query_vector=VECTOR,
            k=4,
            ef_search=200,
            tenant_id=fx.tenant_a,
            visible_chunk_ids=[c.chunk_id for c in chunks],
        )
    assert withheld == 2
    assert denied == sorted([seeded["audit"], seeded["board"]])


async def test_the_executive_is_denied_nothing(fx: Fixture, seeded: dict[str, uuid.UUID]) -> None:
    """The other end of the range: a principal who can read all four is withheld zero.

    This is the case that catches filtering before the LIMIT. "Top-k over everything minus
    what you saw" gives 0 here; "top-k over what you did not see" would happily return the
    next rows and report a non-zero count — a transparency feature lying in the alarming
    direction.
    """
    embedder = _StubEmbedder()
    async with principal_session(fx.executive) as session:
        chunks = await _ann_query(
            session,
            embedder=embedder,  # type: ignore[arg-type]
            query_vector=VECTOR,
            k=4,
            ef_search=200,
            tenant_id=fx.tenant_a,
            principal=fx.executive,
        )
        withheld, denied = await withheld_summary(
            session,
            embedder=embedder,  # type: ignore[arg-type]
            query_vector=VECTOR,
            k=4,
            ef_search=200,
            tenant_id=fx.tenant_a,
            visible_chunk_ids=[c.chunk_id for c in chunks],
        )
    assert len(chunks) == 4
    assert withheld == 0
    assert denied == []


async def test_the_count_never_crosses_a_tenant_boundary(
    fx: Fixture, seeded: dict[str, uuid.UUID]
) -> None:
    """Tenant B has its own document. Counting against the whole database would disclose
    that other corpora exist — a side channel dressed as transparency."""
    embedder = _StubEmbedder()
    async with admin_session() as session:
        chunk = Chunk(
            tenant_id=fx.tenant_b,
            document_id=fx.doc_ids["other_tenant"],
            ordinal=0,
            content="other tenant",
            heading_path=["other"],
            token_count=2,
            sensitivity="internal",
            allowed_groups=["engineering"],
            min_clearance=1,
            embedding_model="bge-small-en-v1.5",
        )
        chunk.embedding_384 = VECTOR
        session.add(chunk)

    async with principal_session(fx.engineer) as session:
        withheld, denied = await withheld_summary(
            session,
            embedder=embedder,  # type: ignore[arg-type]
            query_vector=VECTOR,
            k=10,
            ef_search=200,
            tenant_id=fx.tenant_a,
            visible_chunk_ids=[],
        )
    assert withheld == 4, "tenant A holds four chunks; tenant B's must not be counted"
    assert fx.doc_ids["other_tenant"] not in denied

    async with admin_session() as session:
        await session.execute(delete(Chunk).where(Chunk.tenant_id == fx.tenant_b))


async def test_resolving_a_principal_needs_no_owner_connection(
    fx: Fixture, no_owner_credential: None
) -> None:
    """The caller this file originally missed, and the hottest path there is.

    `auth.resolve()` calls `load_principal` on every request to read the caller's groups
    and clearance from the database — the mechanism behind "a token asserts identity, the
    database owns entitlement". It was doing so over an owner connection.

    The first version of this test file constructed principals directly and never called
    `load_principal`, so it passed while the API still 500'd with the owner URL removed.
    Pointing the credential at a dead host is what found it; this test is what keeps it
    found.
    """
    from sqlalchemy import select as sa_select

    from gatekeeper.core.models import Tenant
    from gatekeeper.ingest.seed import load_principal

    async with principal_session(fx.executive) as session:
        slug = (
            await session.execute(sa_select(Tenant.slug).where(Tenant.id == fx.tenant_a))
        ).scalar_one()

    loaded = await load_principal("engineer", tenant_slug=slug)
    assert loaded.id == fx.engineer.id
    assert sorted(loaded.groups) == sorted(fx.engineer.groups)
    assert int(loaded.clearance) == int(fx.engineer.clearance)


async def test_resolving_an_unknown_handle_raises_rather_than_returning_a_default(
    fx: Fixture, no_owner_credential: None
) -> None:
    """A resolver that quietly returns something for an unknown handle is an
    authentication bypass wearing a convenience feature's clothing."""
    from sqlalchemy import select as sa_select

    from gatekeeper.core.models import Tenant
    from gatekeeper.ingest.seed import load_principal

    async with principal_session(fx.executive) as session:
        slug = (
            await session.execute(sa_select(Tenant.slug).where(Tenant.id == fx.tenant_a))
        ).scalar_one()

    with pytest.raises(LookupError):
        await load_principal("nobody-by-that-name", tenant_slug=slug)


async def test_the_resolver_cannot_be_used_to_enumerate_principals(
    fx: Fixture, no_owner_credential: None
) -> None:
    """`resolve_principal` matches on equality only. A wildcard must find nothing rather
    than walking the directory."""
    from sqlalchemy import select as sa_select

    from gatekeeper.core.models import Tenant
    from gatekeeper.ingest.seed import load_principal

    async with principal_session(fx.executive) as session:
        slug = (
            await session.execute(sa_select(Tenant.slug).where(Tenant.id == fx.tenant_a))
        ).scalar_one()

    for probe in ("%", "_", "engineer%", ""):
        with pytest.raises(LookupError):
            await load_principal(probe, tenant_slug=slug)


async def test_an_out_of_range_k_is_refused_rather_than_clamped(fx: Fixture) -> None:
    """A SECURITY DEFINER function runs owner-side; its inputs are the attack surface."""
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="k out of range"):
        async with principal_session(fx.engineer) as session:
            await withheld_summary(
                session,
                embedder=_StubEmbedder(),  # type: ignore[arg-type]
                query_vector=VECTOR,
                k=100_000,
                ef_search=200,
                tenant_id=fx.tenant_a,
                visible_chunk_ids=[],
            )
