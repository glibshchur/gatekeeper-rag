"""Lexical retrieval and the composed pipeline, under the same RLS policy as the dense path.

The point of these tests is that adding stages cannot widen access. Fusion and reranking
are pure reordering over rows the database already agreed to return, and this asserts that
rather than assuming it — a hybrid retriever whose two halves filtered differently would
be a way to launder access.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete

from gatekeeper.core.db import admin_session, principal_session
from gatekeeper.core.models import Chunk, Document, Tenant
from gatekeeper.core.principal import Clearance, Principal
from gatekeeper.llm.embeddings import Embedder, LocalOnnxEmbedder
from gatekeeper.retrieval.lexical import lexical_topk
from gatekeeper.retrieval.pipeline import (
    BASELINE,
    HYBRID,
    LEXICAL_ONLY,
    RetrievalConfig,
    retrieve,
)

pytestmark = pytest.mark.integration

# "SEC-4417" appears verbatim and has no useful neighbourhood in embedding space: the
# canonical case for keeping a lexical arm at all.
DOCS = [
    (
        "public-onboard",
        "public",
        [],
        0,
        "New joiners finish orientation and security training in their first week.",
    ),
    (
        "staff-ticket",
        "internal",
        ["staff"],
        1,
        "Escalate using ticket reference SEC-4417 when the deploy pipeline stalls.",
    ),
    (
        "board-only",
        "restricted",
        ["board"],
        3,
        "Ticket SEC-4417 was escalated to the board with the full incident timeline.",
    ),
]


@dataclass
class Fx:
    tenant_id: uuid.UUID
    staff: Principal
    director: Principal


@pytest.fixture(scope="module")
def embedder() -> Iterator[Embedder]:
    yield LocalOnnxEmbedder()


@pytest.fixture
async def fx(embedder: Embedder) -> AsyncIterator[Fx]:
    tenant_id = uuid.uuid4()
    vectors = embedder.encode_passages([d[4] for d in DOCS])

    async with admin_session() as session:
        session.add(Tenant(id=tenant_id, slug=f"pipe-{tenant_id.hex[:8]}", name="Pipeline"))
        await session.flush()
        for (name, sens, groups, clr, body), vector in zip(DOCS, vectors, strict=True):
            document = Document(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                source="test",
                path=f"{name}.md",
                title=name,
                content_hash=f"{name:0<64}"[:64],
                sensitivity=sens,
                allowed_groups=groups,
                min_clearance=clr,
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
                sensitivity=sens,
                allowed_groups=groups,
                min_clearance=clr,
                embedding_model=embedder.space.model,
            )
            setattr(chunk, embedder.space.column, vector.tolist())
            session.add(chunk)

    def who(handle: str, groups: list[str], clearance: Clearance) -> Principal:
        return Principal(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            external_id=handle,
            email=f"{handle}@test.invalid",
            groups=groups,
            clearance=clearance,
        )

    yield Fx(
        tenant_id=tenant_id,
        staff=who("staff", ["staff"], Clearance.EMPLOYEE),
        director=who("director", ["staff", "board"], Clearance.EXECUTIVE),
    )

    async with admin_session() as session:
        await session.execute(delete(Chunk).where(Chunk.tenant_id == tenant_id))
        await session.execute(delete(Document).where(Document.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


# --- lexical --------------------------------------------------------------


async def test_lexical_finds_an_exact_identifier(fx: Fx) -> None:
    async with principal_session(fx.director) as session:
        rows = await lexical_topk(session, "SEC-4417", k=10, tenant_id=fx.tenant_id)
    assert {r.title for r in rows} == {"staff-ticket", "board-only"}


async def test_lexical_is_subject_to_the_same_policy_as_dense(fx: Fx) -> None:
    async with principal_session(fx.staff) as session:
        rows = await lexical_topk(session, "SEC-4417", k=10, tenant_id=fx.tenant_id)
    titles = {r.title for r in rows}
    assert "staff-ticket" in titles
    assert "board-only" not in titles, "the lexical arm must not bypass authorization"


async def test_lexical_matches_any_query_term_not_all_of_them(fx: Fx) -> None:
    """`websearch_to_tsquery` ANDs terms, which for a question demands every lexeme in one
    chunk and matches almost nothing. Regression on the OR-of-lexemes fix."""
    question = "What happens during orientation and security training for new joiners?"
    async with principal_session(fx.staff) as session:
        rows = await lexical_topk(session, question, k=10, tenant_id=fx.tenant_id)
    assert "public-onboard" in {r.title for r in rows}


async def test_lexical_survives_a_query_of_only_stop_words(fx: Fx) -> None:
    # Empty lexeme set -> ''::tsquery, which raises unless guarded.
    async with principal_session(fx.staff) as session:
        assert await lexical_topk(session, "the of and a", k=5, tenant_id=fx.tenant_id) == []


async def test_lexical_does_not_raise_on_hostile_punctuation(fx: Fx) -> None:
    async with principal_session(fx.staff) as session:
        await lexical_topk(session, 'unbalanced " quote & | ! (', k=5, tenant_id=fx.tenant_id)


# --- the composed pipeline ------------------------------------------------


async def test_no_configuration_widens_access(fx: Fx, embedder: Embedder) -> None:
    """The load-bearing test for Phase 3. Fusion and reranking reorder rows the database
    already returned, so no arm may reveal `board-only` to staff."""
    for config in (BASELINE, LEXICAL_ONLY, HYBRID):
        result = await retrieve(
            fx.staff,
            "SEC-4417 escalation and incident timeline",
            embedder,
            config=config,
            k=10,
            audit=False,
        )
        titles = {c.title for c in result.chunks}
        assert "board-only" not in titles, f"{config.name} leaked a restricted chunk"


async def test_hybrid_returns_the_union_of_both_arms(fx: Fx, embedder: Embedder) -> None:
    dense = await retrieve(
        fx.director, "orientation training", embedder, config=BASELINE, k=10, audit=False
    )
    lexical = await retrieve(
        fx.director, "orientation training", embedder, config=LEXICAL_ONLY, k=10, audit=False
    )
    hybrid = await retrieve(
        fx.director, "orientation training", embedder, config=HYBRID, k=10, audit=False
    )
    union = {c.chunk_id for c in dense.chunks} | {c.chunk_id for c in lexical.chunks}
    assert {c.chunk_id for c in hybrid.chunks} <= union
    assert {c.chunk_id for c in hybrid.chunks} == union


async def test_a_config_that_retrieves_nothing_is_rejected() -> None:
    with pytest.raises(ValueError, match="retrieves nothing"):
        RetrievalConfig(name="broken", dense=False, lexical=False)


async def test_rerank_without_a_reranker_fails_loudly(fx: Fx, embedder: Embedder) -> None:
    # Silently skipping the stage would make an ablation arm quietly identical to another.
    config = RetrievalConfig(name="needs-reranker", dense=True, rerank=True)
    with pytest.raises(ValueError, match="needs a reranker"):
        await retrieve(fx.staff, "anything", embedder, config=config, audit=False)


async def test_pipeline_writes_one_audit_entry_naming_the_config(
    fx: Fx, embedder: Embedder
) -> None:
    from sqlalchemy import select

    from gatekeeper.core.models import AuditEntry

    await retrieve(fx.staff, "orientation", embedder, config=BASELINE, k=5, audit=True)
    async with admin_session() as session:
        actions = (
            await session.execute(
                select(AuditEntry.action).where(AuditEntry.tenant_id == fx.tenant_id)
            )
        ).all()
        await session.execute(delete(AuditEntry).where(AuditEntry.tenant_id == fx.tenant_id))
    assert [a[0] for a in actions] == ["retrieve:dense"]
