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


def test_claims_contain_only_what_policies_read() -> None:
    claims = json.loads(make(groups=["b", "a"], clearance=Clearance.MANAGER).to_claims())
    assert set(claims) == {"tenant", "principal", "groups", "clearance", "department", "region"}
    # Sorted so the payload is stable and diffable in audit records.
    assert claims["groups"] == ["a", "b"]
    assert claims["clearance"] == 2


def test_employment_type_is_not_leaked_into_claims() -> None:
    # It is not consulted by any policy yet; shipping it would imply otherwise.
    assert "employment_type" not in make(employment_type="contractor").to_claims()


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
