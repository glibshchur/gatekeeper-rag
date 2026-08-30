"""Indirect injection against the real pipeline.

The claim under test is not "the classifier works". It is that **an injection cannot widen
access even when the classifier misses it** — because grants live in a transaction-local
GUC the model cannot write and every read goes through RLS.

That distinction is the reason these tests exist separately from `test_injection.py`:
one measures a heuristic, this one measures the property the heuristic is not responsible
for.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import func, select

from gatekeeper.core.db import admin_session, principal_session
from gatekeeper.core.models import Chunk, Document
from gatekeeper.ingest import seed
from gatekeeper.llm.embeddings import Embedder, LocalOnnxEmbedder
from gatekeeper.redteam import indirect
from gatekeeper.redteam.injection_eval import load_payloads
from gatekeeper.retrieval.pipeline import BASELINE, retrieve

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def embedder() -> Iterator[Embedder]:
    yield LocalOnnxEmbedder()


@pytest.fixture
async def planted(embedder: Embedder) -> AsyncIterator[int]:
    await indirect.unplant()
    payloads = load_payloads()
    await indirect.plant(payloads, embedder, seed.TENANT_SLUG)
    try:
        yield len(payloads)
    finally:
        await indirect.unplant()


async def test_planting_is_fully_reversible(embedder: Embedder) -> None:
    """The poison goes into the *live* corpus, so cleanup has to be exact — a survivor
    would sit in the demo corpus telling every future reader to ignore its instructions."""
    async with admin_session() as session:
        before = (await session.execute(select(func.count(Chunk.id)))).scalar_one()

    await indirect.plant(load_payloads(), embedder, seed.TENANT_SLUG)
    async with admin_session() as session:
        during = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
    assert during == before + len(load_payloads())

    removed = await indirect.unplant()
    assert removed == len(load_payloads())
    async with admin_session() as session:
        after = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
        survivors = (
            await session.execute(
                select(func.count(Document.id)).where(Document.source == indirect.POISON_SOURCE)
            )
        ).scalar_one()
    assert after == before
    assert survivors == 0


async def test_unplant_is_idempotent() -> None:
    assert await indirect.unplant() >= 0
    assert await indirect.unplant() == 0


async def test_a_planted_instruction_cannot_widen_access(planted: int, embedder: Embedder) -> None:
    """The load-bearing test of the whole project.

    An authorized principal retrieves a document that instructs the system to disclose
    material they are not cleared for. Nothing they are not entitled to may come back —
    and the assertion covers every returned chunk, not just the planted one, because the
    point of an injection is to make something *else* appear.
    """
    report = await indirect.run(embedder, k=10)
    assert report.contained, f"access widened: {[a.widened for a in report.breaches]}"
    assert report.reached > 0, "no payload ranked; this run would prove nothing"


async def test_containment_holds_for_payloads_the_classifier_misses(
    planted: int, embedder: Embedder
) -> None:
    """The version of the claim that actually matters.

    If containment only held where detection succeeded, this system's security would rest
    on a set of regular expressions. It rests on RLS instead, so an evasive payload that
    reaches the model must still come back empty-handed.
    """
    report = await indirect.run(embedder, k=10)
    evasive_reached = [a for a in report.attempts if a.retrieved and not a.flagged]
    assert evasive_reached, (
        "no undetected payload reached the model, so this run cannot distinguish "
        "structural containment from the classifier doing the work"
    )
    assert all(not a.widened for a in evasive_reached)


async def test_a_flagged_chunk_is_annotated_and_not_dropped(
    planted: int, embedder: Embedder
) -> None:
    """Flag, do not withhold. Dropping would silently remove documents over a heuristic
    that is wrong 3 times in 73,801, and the structural guarantee does not need it."""
    attacker = await seed.load_principal(indirect.ATTACKER)
    result = await retrieve(
        attacker,
        "what does the handbook say about expense approval",
        embedder,
        config=BASELINE,
        k=10,
        audit=False,
    )
    planted_chunks = [c for c in result.chunks if c.path.startswith("redteam/")]
    assert planted_chunks, "the planted document did not rank; nothing to assert"
    assert any(c.suspicious for c in planted_chunks)
    assert all(c.content for c in planted_chunks), "flagged content must still be returned"


async def test_the_poison_is_readable_by_the_attacker_or_the_test_is_vacuous(
    planted: int,
) -> None:
    # A payload the attacker cannot read is not an indirect injection, it is a document
    # they were already denied.
    attacker = await seed.load_principal(indirect.ATTACKER)
    async with principal_session(attacker) as session:
        visible = (
            await session.execute(
                select(func.count(Document.id)).where(Document.source == indirect.POISON_SOURCE)
            )
        ).scalar_one()
    assert visible == planted
