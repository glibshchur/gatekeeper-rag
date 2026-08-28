"""An independent implementation of the access model, used to judge the SQL one.

This deliberately duplicates ``gatekeeper.authorize()``. Asking the database whether the
database got it right proves nothing: any bug in the policy would be mirrored in the
check. Two implementations of the same written spec, in different languages, disagree
loudly when either drifts — which is the only cheap way to catch an RLS mistake before a
user does.

Keep it a direct transcription of the rules in ADR 0003 and migration 0004. If you find
yourself reaching for the SQL to work out what this should do, the spec is not written
down clearly enough and *that* is the bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from gatekeeper.core.principal import Principal


@dataclass(frozen=True)
class ResourceAttrs:
    """Everything the access model is allowed to consider about a chunk or document."""

    tenant_id: UUID
    sensitivity: str
    allowed_groups: tuple[str, ...]
    min_clearance: int
    need_to_know_tags: tuple[str, ...]
    jurisdiction: tuple[str, ...]


@dataclass(frozen=True)
class DenyRule:
    resource_tags_any: tuple[str, ...]
    unless_groups_any: tuple[str, ...] = ()
    employment_type_in: tuple[str, ...] = ()

    def applies_to(self, principal: Principal) -> bool:
        if self.unless_groups_any and set(self.unless_groups_any) & set(principal.groups):
            return False
        return not (
            self.employment_type_in and principal.employment_type not in self.employment_type_in
        )


def denied_tags(principal: Principal, rules: list[DenyRule]) -> set[str]:
    denied: set[str] = set()
    for rule in rules:
        if rule.applies_to(principal):
            denied |= set(rule.resource_tags_any)
    return denied


def explain(
    principal: Principal,
    resource: ResourceAttrs,
    deny_rules: list[DenyRule] | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return None if access is permitted, else the reason it is refused.

    A reason rather than a bool: when the oracle and the database disagree, "denied" is
    not a useful report. "denied because need-to-know {compensation} is not held" points
    at the rule that differs.
    """
    now = now or datetime.now(UTC)
    rules = deny_rules or []

    if resource.tenant_id != principal.tenant_id:
        return "different tenant"

    if principal.valid_until is not None and principal.valid_until <= now:
        return f"grant expired at {principal.valid_until.isoformat()}"

    blocked = denied_tags(principal, rules) & set(resource.need_to_know_tags)
    if blocked:
        return f"deny rule matches tags {sorted(blocked)}"

    if resource.min_clearance > int(principal.clearance):
        return f"clearance {int(principal.clearance)} below required {resource.min_clearance}"

    if resource.sensitivity != "public" and not (
        set(resource.allowed_groups) & set(principal.groups)
    ):
        return f"no group overlap with {sorted(resource.allowed_groups)}"

    missing = set(resource.need_to_know_tags) - set(principal.need_to_know)
    if missing:
        return f"need-to-know {sorted(missing)} not held"

    if (
        resource.jurisdiction
        and principal.region not in resource.jurisdiction
        and "global" not in principal.need_to_know
    ):
        return f"region {principal.region!r} outside {sorted(resource.jurisdiction)}"

    return None


def is_entitled(
    principal: Principal,
    resource: ResourceAttrs,
    deny_rules: list[DenyRule] | None = None,
    now: datetime | None = None,
) -> bool:
    return explain(principal, resource, deny_rules, now) is None
