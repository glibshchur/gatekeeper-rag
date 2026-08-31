"""The rule that keeps the trace store from becoming an unauthorized mirror of the corpus.

These assert the *shape* of what tracing may record. The interesting one is the last: it
walks the source of every instrumented module rather than trusting that whoever adds the
next span will remember, because a leaked attribute is silent -- it looks like a richer
trace, and the person who can read it is exactly the person the policy is not consulted
about.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from gatekeeper.core import telemetry
from gatekeeper.core.principal import Clearance, Principal

SRC = Path(__file__).resolve().parents[2] / "src" / "gatekeeper"


def _principal() -> Principal:
    return Principal(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        external_id="raj",
        email="raj@test.invalid",
        display_name="Raj",
        groups=["engineering"],
        clearance=Clearance.EMPLOYEE,
    )


def test_span_is_a_null_context_manager_when_tracing_is_off() -> None:
    """The default path must pay nothing. Not a stub object -- nothing."""
    with telemetry.span("retrieve", **{"retrieval.k": 10}) as current:
        assert current is None


@pytest.mark.parametrize("key", sorted(telemetry.FORBIDDEN_KEYS))
def test_content_bearing_attributes_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match="protected content"):
        telemetry.attributes(**{key: "the equity refresh policy"})


def test_refusal_is_loud_rather_than_a_silent_drop() -> None:
    """A quietly discarded attribute is indistinguishable from one never added."""
    with pytest.raises(ValueError):
        telemetry.attributes(**{"query.chars": 42, "query.text": "leak"})


def test_none_valued_attributes_are_dropped_not_recorded() -> None:
    assert telemetry.attributes(a=1, b=None) == {"a": 1}


def test_a_principal_contributes_a_fingerprint_and_no_identity() -> None:
    principal = _principal()
    attrs = telemetry.principal_attrs(principal)
    flat = " ".join(str(v) for v in attrs.values())
    for secret in (principal.external_id, principal.email, principal.display_name):
        assert secret not in flat
    assert len(attrs["entitlement.fingerprint"]) == 16


def test_the_fingerprint_groups_equal_entitlements_and_separates_unequal_ones() -> None:
    """This is the property that makes the fingerprint useful rather than merely safe: two
    people with the same access produce comparable traces."""
    a, b = _principal(), _principal()
    object.__setattr__(b, "tenant_id", a.tenant_id)
    assert (
        telemetry.principal_attrs(a)["entitlement.fingerprint"]
        == telemetry.principal_attrs(b)["entitlement.fingerprint"]
    )

    c = _principal()
    object.__setattr__(c, "tenant_id", a.tenant_id)
    object.__setattr__(c, "clearance", Clearance.EXECUTIVE)
    assert (
        telemetry.principal_attrs(c)["entitlement.fingerprint"]
        != telemetry.principal_attrs(a)["entitlement.fingerprint"]
    )


def _span_keyword_names(path: Path) -> set[str]:
    """Every literal attribute key passed to `span` / `set_attributes` in one module."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"span", "set_attributes", "attributes"}:
            continue
        for kw in node.keywords:
            if kw.arg is not None:
                found.add(kw.arg)
            elif isinstance(kw.value, ast.Dict):
                found.update(k.value for k in kw.value.keys if isinstance(k, ast.Constant))
    return found


def test_no_instrumented_call_site_names_a_forbidden_attribute() -> None:
    """Enforced across the tree, not just at the helper.

    `attributes()` already refuses at runtime, but a span that only executes on the cache
    path would raise for the first time in production. Reading the source finds it now.
    """
    offenders: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        bad = _span_keyword_names(path) & telemetry.FORBIDDEN_KEYS
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    assert not offenders, f"spans would copy protected content into traces: {offenders}"


def test_the_retrieval_pipeline_is_actually_instrumented() -> None:
    """A guard against the opposite failure: the deny list passing because nothing traces."""
    keys = _span_keyword_names(SRC / "retrieval" / "pipeline.py")
    assert {"query.chars", "retrieval.config", "retrieval.withheld"} <= keys
