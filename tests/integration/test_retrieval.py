"""Retrieval tests.

Phase 1's central claim is that authorization happens *inside* the vector scan, not after
it. The corpus below is arranged so that a naive implementation fails loudly: the
restricted document is deliberately the single best semantic match for the query, so if
RLS were bypassed it would come back at rank 1 for everybody.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select

from gatekeeper.core.db import admin_session, principal_session
from gatekeeper.core.models import Chunk, Document, Tenant
from gatekeeper.core.principal import Clearance, Principal, Sensitivity
from gatekeeper.llm.embeddings import Embedder, LocalOnnxEmbedder
from gatekeeper.retrieval.search import search

pytestmark = pytest.mark.integration

QUERY = "how do executive equity refresh grants vest?"

CORPUS = [
    # name          sensitivity              groups        clearance          text
    (
        "expenses",
        Sensitivity.INTERNAL,
        ["staff"],
        Clearance.EMPLOYEE,
        "Employees may expense meals up to 75 USD per day. Receipts are required above "
        "25 USD and must be submitted within 30 days of the expense being incurred.",
    ),
    (
        "onboarding",
        Sensitivity.PUBLIC,
        [],
        Clearance.EXTERNAL,
        "New team members complete orientation in their first week, meet their onboarding "
        "buddy, and finish the security training module before receiving production access.",
    ),
    (
        "equity",
        Sensitivity.RESTRICTED,
        ["board"],
        Clearance.EXECUTIVE,
        "Executive equity refresh grants vest over four years with a one year cliff and are "
        "approved at the January board meeting. Refresh sizing follows the compensation "
        "committee's banding for equity awards.",
    ),
]


@dataclass
class Corpus:
    tenant_id: uuid.UUID
    engineer: Principal
    executive: Principal


@pytest.fixture(scope="module")
def embedder() -> Iterator[Embedder]:
    # Module scope: loading the ONNX session takes seconds and is stateless afterwards.
    yield LocalOnnxEmbedder()


@pytest.fixture
async def corpus(embedder: Embedder) -> AsyncIterator[Corpus]:
    tenant_id = uuid.uuid4()
    vectors = embedder.encode_passages([text for *_, text in CORPUS])

    async with admin_session() as session:
        session.add(Tenant(id=tenant_id, slug=f"ret-{tenant_id.hex[:8]}", name="Retrieval test"))
        await session.flush()

        for (name, sensitivity, groups, clearance, body), vector in zip(
            CORPUS, vectors, strict=True
        ):
            document = Document(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                source="test",
                path=f"{name}.md",
                title=name,
                content_hash=f"{name:0<64}"[:64],
                sensitivity=sensitivity.value,
                allowed_groups=groups,
                min_clearance=int(clearance),
            )
            session.add(document)
            await session.flush()
            chunk = Chunk(
                tenant_id=tenant_id,
                document_id=document.id,
                ordinal=0,
                content=body,
                heading_path=[name],
                token_count=len(body.split()),
                sensitivity=sensitivity.value,
                allowed_groups=groups,
                min_clearance=int(clearance),
                embedding_model=embedder.space.model,
            )
            setattr(chunk, embedder.space.column, vector.tolist())
            session.add(chunk)

    def principal(handle: str, groups: list[str], clearance: Clearance) -> Principal:
        return Principal(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            external_id=handle,
            email=f"{handle}@test.invalid",
            groups=groups,
            clearance=clearance,
        )

    yield Corpus(
        tenant_id=tenant_id,
        engineer=principal("engineer", ["staff"], Clearance.EMPLOYEE),
        executive=principal("executive", ["staff", "board"], Clearance.EXECUTIVE),
    )

    async with admin_session() as session:
        await session.execute(delete(Chunk).where(Chunk.tenant_id == tenant_id))
        await session.execute(delete(Document).where(Document.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def test_the_restricted_document_is_the_best_match_for_the_query(
    corpus: Corpus, embedder: Embedder
) -> None:
    """Establishes the premise. If this fails the other tests prove nothing, because a
    filtered result would be indistinguishable from a result that simply ranked low."""
    result = await search(corpus.executive, QUERY, embedder, k=3)
    assert result.chunks[0].title == "equity"


async def test_authorization_removes_the_top_hit_for_an_unauthorized_principal(
    corpus: Corpus, embedder: Embedder
) -> None:
    result = await search(corpus.engineer, QUERY, embedder, k=3)
    titles = [c.title for c in result.chunks]
    assert "equity" not in titles
    assert titles, "the engineer should still get their own accessible documents"


async def test_the_engineer_and_the_executive_see_different_corpora(
    corpus: Corpus, embedder: Embedder
) -> None:
    engineer = {c.title for c in (await search(corpus.engineer, QUERY, embedder, k=5)).chunks}
    executive = {c.title for c in (await search(corpus.executive, QUERY, embedder, k=5)).chunks}
    assert engineer < executive
    assert executive - engineer == {"equity"}


async def test_withheld_count_reports_what_authorization_removed(
    corpus: Corpus, embedder: Embedder
) -> None:
    result = await search(corpus.engineer, QUERY, embedder, k=3, count_withheld=True)
    assert result.withheld == 1
    unrestricted = await search(corpus.executive, QUERY, embedder, k=3, count_withheld=True)
    assert unrestricted.withheld == 0


async def test_public_documents_are_retrievable_without_group_membership(
    corpus: Corpus, embedder: Embedder
) -> None:
    outsider = Principal(
        id=uuid.uuid4(),
        tenant_id=corpus.tenant_id,
        external_id="outsider",
        email="o@test.invalid",
        groups=[],
        clearance=Clearance.EXTERNAL,
    )
    result = await search(outsider, "what happens during onboarding week?", embedder, k=5)
    assert [c.title for c in result.chunks] == ["onboarding"]


async def test_a_permissive_chunk_under_a_restrictive_document_is_still_denied(
    corpus: Corpus, embedder: Embedder
) -> None:
    """Defence in depth. The denormalised chunk ACL is a performance optimisation; if it
    ever drifts from the document it was copied from, the join to `documents` must be the
    thing that decides. This test corrupts the chunk on purpose."""
    async with admin_session() as session:
        equity_chunk = (
            await session.execute(
                Chunk.__table__.select().where(Chunk.tenant_id == corpus.tenant_id)
            )
        ).all()
        target = next(c for c in equity_chunk if c.heading_path == ["equity"])
        await session.execute(
            Chunk.__table__.update()
            .where(Chunk.id == target.id)
            .values(sensitivity="public", allowed_groups=[], min_clearance=0)
        )

    result = await search(corpus.engineer, QUERY, embedder, k=5)
    assert "equity" not in [c.title for c in result.chunks], (
        "the document-level policy must still deny a chunk whose own ACL was weakened"
    )


async def test_cross_tenant_queries_return_nothing(corpus: Corpus, embedder: Embedder) -> None:
    stranger = Principal(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        external_id="stranger",
        email="s@test.invalid",
        groups=["staff", "board"],
        clearance=Clearance.EXECUTIVE,
    )
    assert (await search(stranger, QUERY, embedder, k=5)).chunks == []


async def test_withheld_count_ignores_other_tenants(corpus: Corpus, embedder: Embedder) -> None:
    """Regression: the withheld count is computed on the admin plane, which has no RLS to
    scope it. An earlier version compared against the entire database, so a tenant with
    three documents was told that other tenants' chunks had been "withheld" from them --
    a side channel disclosing that other corpora exist and roughly how dense they are."""
    neighbour = uuid.uuid4()
    vector = embedder.encode_passages([CORPUS[2][4]])[0]
    async with admin_session() as session:
        session.add(Tenant(id=neighbour, slug=f"nb-{neighbour.hex[:8]}", name="Neighbour"))
        await session.flush()
        document = Document(
            id=uuid.uuid4(),
            tenant_id=neighbour,
            source="test",
            path="neighbour-equity.md",
            title="neighbour-equity",
            content_hash="n" * 64,
            sensitivity=Sensitivity.PUBLIC.value,
            allowed_groups=[],
            min_clearance=0,
        )
        session.add(document)
        await session.flush()
        chunk = Chunk(
            tenant_id=neighbour,
            document_id=document.id,
            ordinal=0,
            content=CORPUS[2][4],
            heading_path=["neighbour-equity"],
            token_count=10,
            sensitivity=Sensitivity.PUBLIC.value,
            allowed_groups=[],
            min_clearance=0,
            embedding_model=embedder.space.model,
        )
        setattr(chunk, embedder.space.column, vector.tolist())
        session.add(chunk)

    try:
        result = await search(corpus.engineer, QUERY, embedder, k=3, count_withheld=True)
        assert result.withheld == 1, "only the in-tenant restricted chunk should count"
        assert "neighbour-equity" not in [c.title for c in result.chunks]
    finally:
        async with admin_session() as session:
            await session.execute(delete(Chunk).where(Chunk.tenant_id == neighbour))
            await session.execute(delete(Document).where(Document.tenant_id == neighbour))
            await session.execute(delete(Tenant).where(Tenant.id == neighbour))


async def test_a_tiny_tenant_still_gets_its_visible_chunks(
    corpus: Corpus, embedder: Embedder
) -> None:
    """Regression, and the nastiest bug in the project so far.

    The ABAC predicate is a function over columns, so Postgres cannot estimate its
    selectivity and assumes the filter is weak — which makes the HNSW index look
    attractive. For a principal who can see two chunks out of 73,797, the graph walk
    never encounters them and the query returns *nothing*, while a plain `SELECT` on the
    same table in the same transaction returns both. No error, no short-return warning:
    retrieval simply goes blind for the most restricted users.

    It appeared only after migration 0007 made `authorize()` cheap enough to inline. The
    expensive opaque version had been pushing the planner to a sequential scan, which is
    exact, so the bug was masked by a performance problem.

    The fix is an explicit `tenant_id = $1` predicate on both retrievers: btree-indexable,
    so the planner can bound a small tenant before ranking it. Redundant for security —
    RLS already enforces the tenant — and load-bearing for recall.
    """
    async with principal_session(corpus.engineer) as session:
        plainly_visible = (
            await session.execute(select(Chunk.id).where(Chunk.tenant_id == corpus.tenant_id))
        ).all()
    assert len(plainly_visible) == 2, "fixture invariant: the engineer may read two chunks"

    result = await search(corpus.engineer, QUERY, embedder, k=3)
    assert len(result.chunks) == 2, (
        "the ANN path returned fewer chunks than the principal can plainly SELECT"
    )
