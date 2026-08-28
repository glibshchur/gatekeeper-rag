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
            },
            separators=(",", ":"),
            sort_keys=True,
        )
