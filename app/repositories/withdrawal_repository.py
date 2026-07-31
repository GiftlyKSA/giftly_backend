"""Courier withdrawal persistence."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from app.models import Withdrawal
from app.models.enums import WithdrawalStatus


class WithdrawalRepository:
    """Creates withdrawal requests and serializes their state transitions."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    def savepoint(self) -> AsyncSessionTransaction:
        """Open a savepoint around an idempotent request write."""
        return self._session.begin_nested()

    async def create(
        self,
        *,
        withdrawal_id: uuid.UUID,
        courier_id: uuid.UUID,
        wallet_id: uuid.UUID,
        amount: Decimal,
        iban_encrypted: str,
        iban_last4: str,
        idempotency_key: str,
    ) -> Withdrawal:
        """Create a held-funds withdrawal request."""
        row = Withdrawal(
            id=withdrawal_id,
            courier_id=courier_id,
            wallet_id=wallet_id,
            amount=amount,
            iban_encrypted=iban_encrypted,
            iban_last4=iban_last4,
            status=WithdrawalStatus.REQUESTED,
            idempotency_key=idempotency_key,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_idempotency(
        self, *, courier_id: uuid.UUID, idempotency_key: str
    ) -> Withdrawal | None:
        """Return a courier's prior response for an idempotent request."""
        row: Withdrawal | None = await self._session.scalar(
            select(Withdrawal).where(
                Withdrawal.courier_id == courier_id,
                Withdrawal.idempotency_key == idempotency_key,
            )
        )
        return row

    async def get_for_update(self, withdrawal_id: uuid.UUID) -> Withdrawal | None:
        """Lock and return a withdrawal so only one admin transition can win."""
        row: Withdrawal | None = await self._session.scalar(
            select(Withdrawal).where(Withdrawal.id == withdrawal_id).with_for_update()
        )
        return row

    async def set_status(
        self,
        row: Withdrawal,
        *,
        status: WithdrawalStatus,
        admin_id: uuid.UUID,
        rejection_reason: str | None = None,
    ) -> None:
        """Apply an admin withdrawal decision to a locked row."""
        row.status = status
        row.processed_by_admin_id = admin_id
        row.rejection_reason = rejection_reason
        await self._session.flush()
