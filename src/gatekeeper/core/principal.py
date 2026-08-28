"""The Principal: who is asking, and what attributes the policy layer may consider.

A Principal is serialised to JSON and pushed into a transaction-local Postgres GUC
(``gatekeeper.principal``) so that row-level security policies can read it. Nothing in
the application layer is trusted to filter rows; see docs/adr/0002.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class Clearance(IntEnum):
    """Ordered clearance levels. Higher dominates lower."""

    EXTERNAL = 0
    EMPLOYEE = 1
    MANAGER = 2
    EXECUTIVE = 3


class Sensitivity(StrEnum):
    """Document sensitivity labels, mirrored by a CHECK constraint in the schema."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class Principal(BaseModel):
    """An authenticated requester and the attributes authorization may consider."""

    id: UUID
    tenant_id: UUID
    external_id: str
    email: str
    display_name: str = ""

    groups: list[str] = Field(default_factory=list)
    clearance: Clearance = Clearance.EMPLOYEE
    department: str | None = None
    region: str | None = None
    employment_type: str = "employee"
    # Need-to-know grants. A resource tagged `compensation` requires the principal to
    # hold `compensation`; the check is subset, not overlap, so a document tagged
    # {pii, compensation} needs both. `global` waives jurisdiction scoping.
    need_to_know: list[str] = Field(default_factory=list)
    valid_until: datetime | None = None

    @property
    def is_expired(self) -> bool:
        if self.valid_until is None:
            return False
        return self.valid_until <= datetime.now(UTC)

    def to_claims(self) -> str:
        """Serialise to the JSON blob that RLS policies read.

        Only attributes the database policies actually consult are included. Keeping
        this payload minimal is deliberate: it is the authorization contract between
        the application and the database, and it should be auditable at a glance.

        Phase 0 shipped six fields because six were enforced. Phase 2 adds
        ``employment_type``, ``need_to_know`` and ``exp`` because the ABAC engine now
        consults all three -- a claim appears here when a policy reads it, never before.

        ``exp`` is belt and braces. This method already refuses to serialise an expired
        grant, but the database re-checks: an application that caches claims, or a bug
        that reuses a Principal built minutes ago, must not outlive the grant.
        """
        if self.is_expired:
            raise PermissionError(f"principal {self.external_id} expired at {self.valid_until}")
        return json.dumps(
            {
                "tenant": str(self.tenant_id),
                "principal": str(self.id),
                "groups": sorted(self.groups),
                "clearance": int(self.clearance),
                "department": self.department,
                "region": self.region,
                "employment_type": self.employment_type,
                "need_to_know": sorted(self.need_to_know),
                "exp": self.valid_until.isoformat() if self.valid_until else None,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
