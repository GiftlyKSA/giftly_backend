"""Dispute persistence (SPEC SECTION 20.H).

A dispute freezes an order's escrow until an admin resolves it. One dispute per order
(the ``uq_disputes_order`` unique constraint), so raising is idempotent per order.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Dispute
from app.models.enums import DisputeStatus


class DisputeRepository:
    """Creates and reads disputes, with FOR UPDATE locking for resolution."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def create(
        self, *, order_id: uuid.UUID, raised_by_user_id: uuid.UUID, reason: str
    ) -> Dispute:
        """Open a dispute for an order."""
        dispute = Dispute(
            order_id=order_id,
            raised_by_user_id=raised_by_user_id,
            reason=reason,
            status=DisputeStatus.OPEN,
        )
        self._session.add(dispute)
        await self._session.flush()
        return dispute

    async def get_for_order(self, order_id: uuid.UUID) -> Dispute | None:
        """Return the order's dispute, or None."""
        result: Dispute | None = await self._session.scalar(
            select(Dispute).where(Dispute.order_id == order_id)
        )
        return result

    async def lock(self, dispute_id: uuid.UUID) -> Dispute | None:
        """Load a dispute FOR UPDATE (resolution serialization)."""
        result: Dispute | None = await self._session.scalar(
            select(Dispute).where(Dispute.id == dispute_id).with_for_update()
        )
        return result

    async def resolve(
        self,
        dispute: Dispute,
        *,
        status: DisputeStatus,
        admin_id: uuid.UUID,
        note: str | None,
        when: datetime,
    ) -> None:
        """Record a dispute's resolution outcome and the resolving admin."""
        dispute.status = status
        dispute.resolved_by_admin_id = admin_id
        dispute.resolution_note = note
        dispute.resolved_at = when
        await self._session.flush()
