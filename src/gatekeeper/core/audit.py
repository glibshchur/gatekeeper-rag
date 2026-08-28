"""Hash-chained audit log.

Every entry commits to its predecessor, so the log is tamper-evident: altering or
deleting any row breaks verification from that point forward. An attacker who can write
to the database can still append, but cannot quietly rewrite history — which is the
realistic threat, because the party most motivated to edit an audit log is usually the
one who already has credentials.

Two properties the implementation depends on:

* **Entries are written in the querying transaction.** The audit row and the query that
  produced it commit or roll back together. A separate connection would let a query
  succeed while its audit entry is lost.
* **The chain is serialised per tenant with an advisory lock.** Two concurrent queries
  reading the same `prev_hash` would fork the chain into two entries claiming the same
  predecessor. The lock is transaction-scoped, so it is released on commit and its blast
  radius is one tenant.

What this does *not* defend against: an attacker with database write access who
recomputes the whole chain from the point of edit forward. Defending against that needs
the head hash published somewhere the database cannot reach — periodic anchoring to an
append-only external store. That is deliberately out of scope here, and saying so is
more useful than implying the chain alone is sufficient.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, text

from gatekeeper.core.models import AuditEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from gatekeeper.core.principal import Principal

GENESIS = "0" * 64


def compute_hash(
    *,
    prev_hash: str,
    tenant_id: UUID,
    principal_id: UUID | None,
    occurred_at: datetime,
    action: str,
    query_text: str | None,
    retrieved: list[UUID],
    denied: list[UUID],
    answer_hash: str | None,
) -> str:
    """Hash one entry over its predecessor.

    Serialisation is canonical -- sorted keys, no insignificant whitespace, UUIDs and
    timestamps as strings -- because a hash chain whose input encoding can vary is a hash
    chain that fails verification for reasons unrelated to tampering.
    """
    payload = json.dumps(
        {
            "prev": prev_hash,
            "tenant": str(tenant_id),
            "principal": str(principal_id) if principal_id else None,
            "at": occurred_at.astimezone(UTC).isoformat(),
            "action": action,
            "query": query_text,
            # Sorted: retrieval order is not a security-relevant fact, and leaving it
            # unsorted would make the hash depend on planner behaviour.
            "retrieved": sorted(str(i) for i in retrieved),
            "denied": sorted(str(i) for i in denied),
            "answer": answer_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def append(
    session: AsyncSession,
    principal: Principal,
    *,
    action: str,
    query_text: str | None = None,
    retrieved: list[UUID] | None = None,
    denied: list[UUID] | None = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    answer_hash: str | None = None,
) -> str:
    """Append one entry inside the caller's transaction. Returns the new head hash."""
    retrieved = retrieved or []
    denied = denied or []

    # Serialise the chain for this tenant. hashtext() gives a stable 32-bit key from the
    # tenant UUID; collisions across tenants would only ever cost a little contention.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": str(principal.tenant_id)}
    )

    prev_hash = (
        await session.execute(
            select(AuditEntry.entry_hash)
            .where(AuditEntry.tenant_id == principal.tenant_id)
            .order_by(AuditEntry.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or GENESIS

    occurred_at = datetime.now(UTC)
    entry_hash = compute_hash(
        prev_hash=prev_hash,
        tenant_id=principal.tenant_id,
        principal_id=principal.id,
        occurred_at=occurred_at,
        action=action,
        query_text=query_text,
        retrieved=retrieved,
        denied=denied,
        answer_hash=answer_hash,
    )

    session.add(
        AuditEntry(
            tenant_id=principal.tenant_id,
            principal_id=principal.id,
            occurred_at=occurred_at,
            action=action,
            query_text=query_text,
            retrieved_doc_ids=retrieved,
            denied_doc_ids=denied,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            answer_hash=answer_hash,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
    )
    return entry_hash


class ChainBreakError(Exception):
    """Raised with the id of the first entry that does not verify."""

    def __init__(self, entry_id: int, reason: str) -> None:
        super().__init__(f"audit chain broken at entry {entry_id}: {reason}")
        self.entry_id = entry_id
        self.reason = reason


async def verify_chain(session: AsyncSession, tenant_id: UUID) -> int:
    """Walk a tenant's chain from genesis. Returns the number of entries verified.

    Raises :class:`ChainBreakError` on the first entry whose recorded hash disagrees with its
    contents, or whose `prev_hash` does not match the entry before it. Both failures are
    reported because they catch different things: a content mismatch means a row was
    edited, a link mismatch means one was deleted or inserted.
    """
    entries = (
        (
            await session.execute(
                select(AuditEntry)
                .where(AuditEntry.tenant_id == tenant_id)
                .order_by(AuditEntry.id.asc())
            )
        )
        .scalars()
        .all()
    )

    expected_prev = GENESIS
    for entry in entries:
        if entry.prev_hash != expected_prev:
            raise ChainBreakError(entry.id, "link does not match the preceding entry")
        recomputed = compute_hash(
            prev_hash=entry.prev_hash or GENESIS,
            tenant_id=entry.tenant_id,
            principal_id=entry.principal_id,
            occurred_at=entry.occurred_at,
            action=entry.action,
            query_text=entry.query_text,
            retrieved=list(entry.retrieved_doc_ids),
            denied=list(entry.denied_doc_ids),
            answer_hash=entry.answer_hash,
        )
        if recomputed != entry.entry_hash:
            raise ChainBreakError(entry.id, "contents do not match the recorded hash")
        expected_prev = entry.entry_hash

    return len(entries)
