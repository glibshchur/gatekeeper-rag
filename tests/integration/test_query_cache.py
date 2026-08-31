"""Query cache.

The cache is the most natural place in this system to reintroduce the bug the rest of it
prevents, so the tests are weighted accordingly: one checks that it hits, and five check
that it cannot leak.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, func, select

from gatekeeper.core.db import admin_session, principal_session
from gatekeeper.core.models import Chunk, Document, QueryCacheEntry, Tenant
from gatekeeper.core.principal import Clearance, Principal
from gatekeeper.llm.embeddings import Embedder, LocalOnnxEmbedder
from gatekeeper.retrieval import cache
from gatekeeper.retrieval.pipeline import BASELINE, retrieve

pytestmark = pytest.mark.integration

QUERY = "how do executive equity refresh grants vest"
DOCS = [
    (
        "open-onboarding",
        "public",
        [],
        0,
        "New joiners complete orientation and security training in their first week.",
    ),
    (
        "staff-expenses",
        "internal",
        ["staff"],
        1,
        "Employees may expense meals up to 75 USD per day with receipts.",
    ),
    (
        "board-equity",
        "restricted",
        ["board"],
        3,
        "Executive equity refresh grants vest over four years with a one year cliff.",
    ),
]


@dataclass
class Fx:
    tenant_id: uuid.UUID
    staff: Principal
    twin: Principal
    director: Principal
    secret_chunk_id: uuid.UUID


@pytest.fixture(scope="module")
def embedder() -> Iterator[Embedder]:
    yield LocalOnnxEmbedder()


@pytest.fixture
async def fx(embedder: Embedder) -> AsyncIterator[Fx]:
    tenant_id = uuid.uuid4()
    vectors = embedder.encode_passages([d[4] for d in DOCS])
    secret_chunk_id = uuid.uuid4()

    async with admin_session() as session:
        session.add(Tenant(id=tenant_id, slug=f"cache-{tenant_id.hex[:8]}", name="Cache"))
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
                id=secret_chunk_id if name == "board-equity" else uuid.uuid4(),
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

    def who(handle: str, groups: list[str], clearance: Clearance, **kw: object) -> Principal:
        base: dict[str, object] = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "external_id": handle,
            "email": f"{handle}@test.invalid",
            "groups": groups,
            "clearance": clearance,
        }
        return Principal(**(base | kw))  # type: ignore[arg-type]

    yield Fx(
        tenant_id=tenant_id,
        staff=who("staff", ["staff"], Clearance.EMPLOYEE),
        # Same entitlements, different person: must share cache entries.
        twin=who("twin", ["staff"], Clearance.EMPLOYEE),
        director=who("director", ["staff", "board"], Clearance.EXECUTIVE),
        secret_chunk_id=secret_chunk_id,
    )

    async with admin_session() as session:
        await session.execute(delete(Chunk).where(Chunk.tenant_id == tenant_id))
        await session.execute(delete(Document).where(Document.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.execute(delete(QueryCacheEntry))


# --- the fingerprint -------------------------------------------------------


def test_identical_entitlements_share_a_fingerprint(fx: Fx) -> None:
    """The whole reason this is keyed on entitlement rather than identity: hundreds of
    people share a role, and a per-person cache would never hit."""
    assert cache.entitlement_fingerprint(fx.staff, 1) == cache.entitlement_fingerprint(fx.twin, 1)
    assert fx.staff.id != fx.twin.id


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("clearance", Clearance.EXECUTIVE),
        ("groups", ["staff", "board"]),
        ("need_to_know", ["compensation"]),
        ("region", "NL"),
        ("employment_type", "contractor"),
        ("tenant_id", uuid.uuid4()),
    ],
)
def test_fingerprint_covers_every_claim_the_policy_reads(
    fx: Fx, attribute: str, value: object
) -> None:
    """Each of these is consulted by `gatekeeper.authorize()`. If one were missing from
    the fingerprint, two principals differing only in it would share cache entries — which
    is precisely the cross-principal leak this design exists to rule out."""
    altered = fx.staff.model_copy(update={attribute: value})
    assert cache.entitlement_fingerprint(fx.staff, 1) != cache.entitlement_fingerprint(altered, 1)


def test_the_epoch_is_part_of_the_key(fx: Fx) -> None:
    assert cache.entitlement_fingerprint(fx.staff, 1) != cache.entitlement_fingerprint(fx.staff, 2)


# --- hits and misses -------------------------------------------------------


async def test_a_repeated_query_hits_and_returns_the_same_documents(
    fx: Fx, embedder: Embedder
) -> None:
    cold = await retrieve(
        fx.staff, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True
    )
    assert not cold.cached

    warm = await retrieve(
        fx.staff, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True
    )
    assert warm.cached
    assert warm.cached_query == QUERY
    assert [c.chunk_id for c in warm.chunks] == [c.chunk_id for c in cold.chunks]


async def test_a_twin_principal_reuses_the_entry(fx: Fx, embedder: Embedder) -> None:
    await retrieve(fx.staff, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True)
    warm = await retrieve(
        fx.twin, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True
    )
    assert warm.cached, "identical entitlements should share the cache"


async def test_a_more_privileged_principal_does_not_reuse_the_entry(
    fx: Fx, embedder: Embedder
) -> None:
    """The leak, if the key were wrong in the other direction: staff's narrower results
    served to a director, or worse, the reverse."""
    await retrieve(fx.director, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True)
    result = await retrieve(
        fx.staff, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True
    )
    assert not result.cached
    assert fx.secret_chunk_id not in {uuid.UUID(c.chunk_id) for c in result.chunks}


async def test_bumping_the_epoch_retires_the_cache(fx: Fx, embedder: Embedder) -> None:
    await retrieve(fx.staff, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True)
    assert (
        await retrieve(fx.staff, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True)
    ).cached

    await cache.bump_epoch("test")
    assert not (
        await retrieve(fx.staff, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True)
    ).cached


# --- the defence that does not rely on the key being right ----------------


async def test_a_forged_entry_cannot_leak_because_hits_are_re_authorized(
    fx: Fx, embedder: Embedder
) -> None:
    """The test that matters most.

    A cache entry is written *directly* into the staff principal's own bucket containing
    the id of a restricted chunk — simulating a fingerprint bug, a hash collision, or a
    hand-edited row. The hit is served, and it still comes back empty, because
    `fetch_by_ids` re-runs the query through RLS. Correctness does not depend on the key
    being right; the key only decides how often the cache is useful.
    """
    epoch = await cache.current_epoch()
    vector = embedder.encode_query(QUERY).tolist()
    await cache.store(fx.staff, QUERY, vector, [fx.secret_chunk_id], embedder, epoch)

    result = await retrieve(
        fx.staff, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True
    )
    assert result.cached, "the forged entry should have been hit"
    assert result.chunks == [], "a re-authorized hit must return nothing forbidden"


async def test_the_cache_stores_ids_and_never_content(fx: Fx, embedder: Embedder) -> None:
    """A cache holding content would be a second copy of the corpus outside RLS."""
    await retrieve(fx.staff, QUERY, embedder, config=BASELINE, k=5, audit=False, use_cache=True)
    async with admin_session() as session:
        entry = (await session.execute(select(QueryCacheEntry))).scalars().first()
    assert entry is not None
    assert entry.chunk_ids
    columns = {c.name for c in QueryCacheEntry.__table__.columns}
    assert "content" not in columns and "chunks" not in columns


async def test_the_app_role_cannot_read_the_cache() -> None:
    """The cache is admin-plane only. It holds no tenant data, and giving the query plane
    access to it would create a path to chunk ids that never passes through a policy."""
    async with principal_session(
        Principal(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            external_id="probe",
            email="p@test.invalid",
            groups=[],
            clearance=Clearance.EXTERNAL,
        )
    ) as session:
        with pytest.raises(Exception, match="permission denied"):
            await session.execute(select(func.count(QueryCacheEntry.id)))
