"""MCP tool behaviour.

An agent surface is where an access model is most likely to be quietly bypassed: the
tools are new code, the caller is a language model, and the output is prose rather than
rows. These tests assert the two properties that matter for that surface — the tools
enforce the same policy as everything else, and their *text* does not leak what the
policy withheld.

The tool bodies are exercised directly rather than over stdio. A subprocess and a
JSON-RPC round trip per assertion would cost a model load each and test the MCP SDK
rather than this project; `build_server()` is a thin registration layer over these
functions and is covered by the manual smoke run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete

from gatekeeper.apps.mcp.server import Runtime, do_get_document, do_search, do_whoami, load_runtime
from gatekeeper.core.db import admin_session
from gatekeeper.core.models import Chunk, Document, Tenant
from gatekeeper.core.principal import Clearance, Principal
from gatekeeper.llm.embeddings import Embedder, LocalOnnxEmbedder
from gatekeeper.retrieval.pipeline import BASELINE

pytestmark = pytest.mark.integration

SECRET_TITLE = "board-equity-plan"
DOCS = [
    (
        "onboarding-guide",
        "public",
        [],
        0,
        "New joiners complete orientation and security training in their first week.",
    ),
    (
        "engineering-runbook",
        "internal",
        ["staff"],
        1,
        "Deploys are gated on a green pipeline; escalate a stalled deploy to the on-call.",
    ),
    (
        SECRET_TITLE,
        "restricted",
        ["board"],
        3,
        "The executive equity refresh plan grants options vesting over four years.",
    ),
]


@dataclass
class Fx:
    tenant_id: uuid.UUID
    staff: Runtime
    director: Runtime


@pytest.fixture(scope="module")
def embedder() -> Iterator[Embedder]:
    yield LocalOnnxEmbedder()


@pytest.fixture
async def fx(embedder: Embedder) -> AsyncIterator[Fx]:
    tenant_id = uuid.uuid4()
    vectors = embedder.encode_passages([d[4] for d in DOCS])

    async with admin_session() as session:
        session.add(Tenant(id=tenant_id, slug=f"mcp-{tenant_id.hex[:8]}", name="MCP"))
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

    def rt(handle: str, groups: list[str], clearance: Clearance) -> Runtime:
        return Runtime(
            principal=Principal(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                external_id=handle,
                email=f"{handle}@test.invalid",
                display_name=handle,
                groups=groups,
                clearance=clearance,
            ),
            embedder=embedder,
            reranker=None,
            config=BASELINE,
        )

    yield Fx(
        tenant_id=tenant_id,
        staff=rt("staff", ["staff"], Clearance.EMPLOYEE),
        director=rt("director", ["staff", "board"], Clearance.EXECUTIVE),
    )

    async with admin_session() as session:
        await session.execute(delete(Chunk).where(Chunk.tenant_id == tenant_id))
        await session.execute(delete(Document).where(Document.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


QUERY = "executive equity refresh plan and option vesting"


# --- authorization --------------------------------------------------------


async def test_search_enforces_the_same_policy_as_every_other_surface(fx: Fx) -> None:
    assert SECRET_TITLE in await do_search(fx.director, QUERY)
    assert SECRET_TITLE not in await do_search(fx.staff, QUERY)


async def test_a_withheld_result_is_reported_as_a_count_not_an_identity(fx: Fx) -> None:
    """The rule that makes the transparency signal safe.

    "1 additional match exists" tells the model the answer may be incomplete. Naming the
    document would hand it the title — and for restricted material the title is usually
    the secret. The model will relay whatever it is given.
    """
    text = await do_search(fx.staff, QUERY)
    assert "not\nauthorised to read" in text or "not authorised to read" in text
    assert SECRET_TITLE not in text
    assert "board-equity" not in text


async def test_search_says_so_rather_than_returning_a_bare_empty_result(fx: Fx) -> None:
    outsider = Runtime(
        principal=fx.staff.principal.model_copy(
            update={"groups": [], "clearance": Clearance.EXTERNAL}
        ),
        embedder=fx.staff.embedder,
        reranker=None,
        config=BASELINE,
    )
    text = await do_search(outsider, "deploy pipeline escalation runbook")
    # Public material is still reachable, so this asserts the shape, not emptiness.
    assert "Searched as" in text


# --- get_document ---------------------------------------------------------


async def test_get_document_serves_an_authorised_path(fx: Fx) -> None:
    text = await do_get_document(fx.director, f"{SECRET_TITLE}.md")
    assert "vesting over four years" in text
    assert 'sensitivity="restricted"' in text


async def test_unreadable_and_nonexistent_paths_are_indistinguishable(fx: Fx) -> None:
    """Otherwise the tool is an oracle: an agent could enumerate the corpus by probing
    paths and reading which error it gets back."""
    forbidden = await do_get_document(fx.staff, f"{SECRET_TITLE}.md")
    missing = await do_get_document(fx.staff, "no/such/document.md")
    assert forbidden == missing.replace("no/such/document.md", f"{SECRET_TITLE}.md")
    assert "is available to this principal" in forbidden


# --- whoami ---------------------------------------------------------------


async def test_whoami_describes_reach_without_naming_what_is_out_of_reach(fx: Fx) -> None:
    text = await do_whoami(fx.staff)
    assert "staff" in text
    assert "restricted 0" in text, "the staff principal reaches no restricted chunks"
    assert SECRET_TITLE not in text


# --- startup ---------------------------------------------------------------


async def test_the_server_refuses_to_start_for_an_expired_grant() -> None:
    """Failing at startup names the problem. A server that starts and then refuses every
    call looks like an empty corpus, and an agent will report it as one."""
    async with admin_session() as session:
        await session.execute(
            Document.__table__.select().limit(0)
        )  # keep the session shape consistent with other tests
    from gatekeeper.ingest import seed

    original = await seed.load_principal("wren")
    assert original.is_expired, "fixture invariant: wren's grant has lapsed"
    with pytest.raises(SystemExit, match="expired grant"):
        await load_runtime("wren", with_reranker=False)


async def test_a_runtime_without_a_reranker_uses_a_config_that_does_not_need_one(
    fx: Fx,
) -> None:
    """Regression. The default config requires a cross-encoder, so a server started with
    --no-rerank that kept the default raised on every single search — it started cleanly
    and then failed every call, which is the worst of both."""
    assert fx.staff.config.rerank is False
    assert await do_search(fx.staff, "orientation") != ""
