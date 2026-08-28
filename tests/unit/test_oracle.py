"""The oracle is the thing that judges the database, so it gets tested on its own first.

If these tests and the SQL both encode the same misunderstanding, the reconciliation in
the red-team suite passes and proves nothing. So these assert against the *written rules*
in ADR 0003 -- clearance is a ceiling, need-to-know is subset, deny beats allow -- rather
than against whatever the policy happens to do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from gatekeeper.core.principal import Clearance, Principal
from gatekeeper.redteam.oracle import DenyRule, ResourceAttrs, explain, is_entitled

TENANT = uuid4()


def principal(**kwargs: object) -> Principal:
    base: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": TENANT,
        "external_id": "p",
        "email": "p@test.invalid",
        "groups": ["staff"],
        "clearance": Clearance.EMPLOYEE,
    }
    return Principal(**(base | kwargs))  # type: ignore[arg-type]


def resource(**kwargs: object) -> ResourceAttrs:
    base: dict[str, object] = {
        "tenant_id": TENANT,
        "sensitivity": "internal",
        "allowed_groups": ("staff",),
        "min_clearance": 1,
        "need_to_know_tags": (),
        "jurisdiction": (),
    }
    return ResourceAttrs(**(base | kwargs))  # type: ignore[arg-type]


# --- the baseline rules ----------------------------------------------------


def test_matching_group_and_clearance_is_allowed() -> None:
    assert explain(principal(), resource()) is None


def test_tenant_mismatch_is_refused_first() -> None:
    assert explain(principal(), resource(tenant_id=uuid4())) == "different tenant"


def test_clearance_is_a_ceiling_not_a_key() -> None:
    # Top clearance, wrong group: refused. This is the property that makes the model
    # ABAC rather than a classification hierarchy.
    top = principal(clearance=Clearance.EXECUTIVE, groups=["nobody"])
    assert "no group overlap" in (explain(top, resource(min_clearance=0)) or "")


def test_clearance_floor_is_enforced() -> None:
    reason = explain(principal(clearance=Clearance.EMPLOYEE), resource(min_clearance=3))
    assert reason == "clearance 1 below required 3"


def test_public_material_needs_no_group() -> None:
    stranger = principal(groups=[])
    assert explain(stranger, resource(sensitivity="public", allowed_groups=())) is None


# --- need-to-know ----------------------------------------------------------


def test_need_to_know_is_subset_not_overlap() -> None:
    """A resource tagged {pii, compensation} needs BOTH grants. Overlap semantics would
    mean any single tag unlocks it, which inverts the meaning of need-to-know."""
    partial = principal(need_to_know=["pii"])
    reason = explain(partial, resource(need_to_know_tags=("pii", "compensation")))
    assert reason == "need-to-know ['compensation'] not held"

    complete = principal(need_to_know=["pii", "compensation"])
    assert explain(complete, resource(need_to_know_tags=("pii", "compensation"))) is None


def test_untagged_resources_need_no_grants() -> None:
    assert explain(principal(need_to_know=[]), resource(need_to_know_tags=())) is None


# --- jurisdiction ----------------------------------------------------------


def test_jurisdiction_scopes_by_region() -> None:
    nl = principal(region="NL")
    assert explain(nl, resource(jurisdiction=("NL",))) is None
    assert "outside" in (explain(nl, resource(jurisdiction=("IN",))) or "")


def test_global_grant_waives_jurisdiction() -> None:
    # People Ops must read every entity's policy, not only the one they sit in.
    roaming = principal(region="NL", need_to_know=["global"])
    assert explain(roaming, resource(jurisdiction=("IN", "FR"))) is None


def test_no_jurisdiction_means_unscoped() -> None:
    assert explain(principal(region=None), resource(jurisdiction=())) is None


# --- expiry ----------------------------------------------------------------


def test_expired_grant_refuses_everything_including_public() -> None:
    lapsed = principal(valid_until=datetime.now(UTC) - timedelta(seconds=1))
    reason = explain(lapsed, resource(sensitivity="public", allowed_groups=()))
    assert reason is not None and "expired" in reason


def test_future_expiry_is_live() -> None:
    live = principal(valid_until=datetime.now(UTC) + timedelta(days=1))
    assert explain(live, resource()) is None


# --- deny rules ------------------------------------------------------------


def test_deny_beats_allow() -> None:
    """The principal satisfies every allow condition and is still refused."""
    rule = DenyRule(resource_tags_any=("legal",), unless_groups_any=("legal-team",))
    holder = principal(clearance=Clearance.EXECUTIVE, need_to_know=["legal"])
    res = resource(need_to_know_tags=("legal",), min_clearance=0)
    assert is_entitled(holder, res) is True, "without the rule this must be allowed"
    assert "deny rule matches" in (explain(holder, res, [rule]) or "")


def test_deny_rule_exemption_by_group() -> None:
    rule = DenyRule(resource_tags_any=("legal",), unless_groups_any=("legal-team",))
    lawyer = principal(groups=["staff", "legal-team"], need_to_know=["legal"])
    assert explain(lawyer, resource(need_to_know_tags=("legal",)), [rule]) is None


def test_deny_rule_scoped_by_employment_type() -> None:
    rule = DenyRule(resource_tags_any=("pii",), employment_type_in=("contractor",))
    res = resource(need_to_know_tags=("pii",))
    staff = principal(employment_type="employee", need_to_know=["pii"])
    contractor = principal(employment_type="contractor", need_to_know=["pii"])
    assert explain(staff, res, [rule]) is None
    assert "deny rule matches" in (explain(contractor, res, [rule]) or "")


def test_deny_is_evaluated_before_clearance() -> None:
    # Ordering matters for the *reason*, which is what makes a disagreement debuggable.
    rule = DenyRule(resource_tags_any=("pii",))
    reason = explain(principal(), resource(need_to_know_tags=("pii",), min_clearance=3), [rule])
    assert reason is not None and reason.startswith("deny rule")
