"""Read-only aggregate queries for the admin dashboard (SPEC SECTION 18.3).

These back the overview and the list/detail pages. Kept in the repository layer so the
admin service — and therefore the dashboard — never issues a raw query itself.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Dispute,
    Invoice,
    Order,
    PaymentIntent,
    Wallet,
    Withdrawal,
)
from app.models.enums import (
    DisputeStatus,
    OrderStatus,
    WithdrawalStatus,
)


class AdminReadRepository:
    """Aggregate and list reads across orders, invoices, disputes, and money."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def order_counts_by_status(self) -> dict[str, int]:
        """Return a map of order status -> count."""
        rows = await self._session.execute(
            select(Order.status, func.count()).group_by(Order.status)
        )
        return {str(status): count for status, count in rows.all()}

    async def open_dispute_count(self) -> int:
        """Return the number of open disputes."""
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(Dispute)
                .where(Dispute.status == DisputeStatus.OPEN)
            )
        ) or 0

    async def pending_withdrawal_count(self) -> int:
        """Return the number of withdrawals awaiting processing."""
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(Withdrawal)
                .where(Withdrawal.status == WithdrawalStatus.REQUESTED)
            )
        ) or 0

    async def system_wallet_balances(self) -> dict[str, Decimal]:
        """Return a map of system wallet type -> balance."""
        rows = await self._session.execute(
            select(Wallet.type, Wallet.balance).where(Wallet.user_id.is_(None))
        )
        return {str(wtype): balance for wtype, balance in rows.all()}

    async def list_orders(self, status: OrderStatus | None = None, limit: int = 50) -> list[Order]:
        """Return orders, newest first, optionally filtered by status."""
        query = select(Order).order_by(Order.created_at.desc()).limit(limit)
        if status is not None:
            query = query.where(Order.status == status)
        return list(await self._session.scalars(query))

    async def get_order(self, order_id: uuid.UUID) -> Order | None:
        """Return an order by id, or None."""
        return await self._session.get(Order, order_id)

    async def list_invoices(self, limit: int = 50) -> list[Invoice]:
        """Return invoices, newest first."""
        return list(
            await self._session.scalars(
                select(Invoice).order_by(Invoice.created_at.desc()).limit(limit)
            )
        )

    async def get_invoice(self, invoice_id: uuid.UUID) -> Invoice | None:
        """Return an invoice by id, or None."""
        return await self._session.get(Invoice, invoice_id)

    async def list_disputes(
        self, status: DisputeStatus | None = None, limit: int = 50
    ) -> list[Dispute]:
        """Return disputes, newest first, optionally filtered by status."""
        query = select(Dispute).order_by(Dispute.created_at.desc()).limit(limit)
        if status is not None:
            query = query.where(Dispute.status == status)
        return list(await self._session.scalars(query))

    async def get_dispute(self, dispute_id: uuid.UUID) -> Dispute | None:
        """Return a dispute by id, or None."""
        return await self._session.get(Dispute, dispute_id)

    async def list_withdrawals(
        self, status: WithdrawalStatus | None = None, limit: int = 50
    ) -> list[Withdrawal]:
        """Return withdrawals, newest first, optionally filtered by status."""
        query = select(Withdrawal).order_by(Withdrawal.created_at.desc()).limit(limit)
        if status is not None:
            query = query.where(Withdrawal.status == status)
        return list(await self._session.scalars(query))

    async def get_withdrawal(self, withdrawal_id: uuid.UUID) -> Withdrawal | None:
        """Return a withdrawal by id, or None."""
        return await self._session.get(Withdrawal, withdrawal_id)

    async def list_wallets(self, limit: int = 50) -> list[Wallet]:
        """Return system wallets and a page of user wallets, system first."""
        query = (
            select(Wallet)
            .order_by(Wallet.user_id.is_(None).desc(), Wallet.created_at.desc())
            .limit(limit)
        )
        return list(await self._session.scalars(query))

    async def get_wallet(self, wallet_id: uuid.UUID) -> Wallet | None:
        """Return a wallet by id, or None."""
        return await self._session.get(Wallet, wallet_id)

    async def list_topups(self, limit: int = 50) -> list[PaymentIntent]:
        """Return wallet top-up intents, newest first."""
        query = (
            select(PaymentIntent)
            .where(PaymentIntent.purpose == "WALLET_TOPUP")
            .order_by(PaymentIntent.created_at.desc())
            .limit(limit)
        )
        return list(await self._session.scalars(query))
