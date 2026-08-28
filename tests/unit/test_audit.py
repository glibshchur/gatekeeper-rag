"""Hash-chain arithmetic. The database-backed behaviour is in the integration suite."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from gatekeeper.core.audit import GENESIS, compute_hash

TENANT, PRINCIPAL = uuid4(), uuid4()
WHEN = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def h(**kwargs: object) -> str:
    base: dict[str, object] = {
        "prev_hash": GENESIS,
        "tenant_id": TENANT,
        "principal_id": PRINCIPAL,
        "occurred_at": WHEN,
        "action": "search",
        "query_text": "expense policy",
        "retrieved": [],
        "denied": [],
        "answer_hash": None,
    }
    return compute_hash(**(base | kwargs))  # type: ignore[arg-type]


def test_hashing_is_deterministic() -> None:
    assert h() == h()


def test_every_field_is_committed_to() -> None:
    # If a field is not in the hash, an attacker can edit it without breaking the chain.
    baseline = h()
    assert h(action="export") != baseline
    assert h(query_text="something else") != baseline
    assert h(principal_id=uuid4()) != baseline
    assert h(tenant_id=uuid4()) != baseline
    assert h(occurred_at=datetime(2026, 8, 28, 12, 0, 1, tzinfo=UTC)) != baseline
    assert h(retrieved=[uuid4()]) != baseline
    assert h(denied=[uuid4()]) != baseline
    assert h(answer_hash="deadbeef") != baseline
    assert h(prev_hash="a" * 64) != baseline


def test_document_order_does_not_affect_the_hash() -> None:
    """Retrieval order is not a security-relevant fact, and leaving it unsorted would
    make verification depend on planner behaviour."""
    a, b = uuid4(), uuid4()
    assert h(retrieved=[a, b]) == h(retrieved=[b, a])


def test_timezone_representation_does_not_affect_the_hash() -> None:
    from datetime import timedelta, timezone

    same_instant = WHEN.astimezone(timezone(timedelta(hours=5)))
    assert h(occurred_at=same_instant) == h()


def test_chaining_makes_each_entry_depend_on_its_predecessor() -> None:
    first = h()
    second = h(prev_hash=first, action="export")
    # Re-deriving the second entry over a forged predecessor cannot reproduce it.
    assert h(prev_hash="0" * 64, action="export") != second
