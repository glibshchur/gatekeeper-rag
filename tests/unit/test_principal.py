from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from gatekeeper.core.principal import Clearance, Principal


def make(**kwargs: object) -> Principal:
    base: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "external_id": "test",
        "email": "test@example.com",
    }
    return Principal(**(base | kwargs))  # type: ignore[arg-type]


def test_claims_contain_exactly_what_policies_read() -> None:
    """The claims payload is the authorization contract between the application and the
    database. A field appears here when a policy consults it and not before -- shipping
    an unused attribute implies an enforcement that does not exist.

    `employment_type`, `need_to_know` and `exp` joined in Phase 2, when the ABAC engine
    started reading all three. Before that they were deliberately withheld.
    """
    claims = json.loads(make(groups=["b", "a"], clearance=Clearance.MANAGER).to_claims())
    assert set(claims) == {
        "tenant",
        "principal",
        "groups",
        "clearance",
        "department",
        "region",
        "employment_type",
        "need_to_know",
        "exp",
    }
    # Sorted so the payload is stable and diffable in audit records.
    assert claims["groups"] == ["a", "b"]
    assert claims["clearance"] == 2


def test_need_to_know_grants_are_sorted_and_present() -> None:
    claims = json.loads(make(need_to_know=["pii", "compensation"]).to_claims())
    assert claims["need_to_know"] == ["compensation", "pii"]


def test_expiry_is_published_to_the_database_not_only_checked_here() -> None:
    """to_claims() refuses to serialise a lapsed grant, but an application that caches
    claims would outlive it. The database re-checks `exp`, so it has to be in the blob."""
    from datetime import timedelta

    future = datetime.now(UTC) + timedelta(days=1)
    claims = json.loads(make(valid_until=future).to_claims())
    assert claims["exp"] == future.isoformat()
    assert json.loads(make().to_claims())["exp"] is None


def test_expired_grants_cannot_produce_claims() -> None:
    expired = make(valid_until=datetime.now(UTC) - timedelta(seconds=1))
    assert expired.is_expired
    with pytest.raises(PermissionError, match="expired"):
        expired.to_claims()


def test_future_expiry_is_fine() -> None:
    live = make(valid_until=datetime.now(UTC) + timedelta(days=1))
    assert not live.is_expired
    assert live.to_claims()


def test_clearance_is_ordered() -> None:
    assert Clearance.EXTERNAL < Clearance.EMPLOYEE < Clearance.MANAGER < Clearance.EXECUTIVE
