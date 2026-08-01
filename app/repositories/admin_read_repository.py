"""Read-only aggregate queries for the admin dashboard (SPEC SECTION 18.3).

These back the overview and the list/detail pages. Kept in the repository layer so the
admin service — and therefore the dashboard — never issues a raw query itself.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

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
from app.models.base import Base
from app.models.enums import (
    DisputeStatus,
    OrderStatus,
    WithdrawalStatus,
)

_PAGE_SIZE = 50
_EDITABLE_TABLES = frozenset({"users", "courier_profiles", "orders"})
_EDIT_URL_COLUMNS = {"users": "id", "courier_profiles": "user_id", "orders": "id"}
_REDACTED_MARKERS = (
    "encrypted",
    "token",
    "secret",
    "hash",
    "fingerprint",
    "password",
)
_REDACTED_COLUMNS = {
    "phone",
    "email",
    "full_name",
    "date_of_birth",
    "ip_address",
    "user_agent",
    "delivery_address_note",
    "iban_last4",
    "paylink_url",
}


@dataclass(frozen=True)
class AdminTableInfo:
    """A dashboard-visible application table and its permitted interaction mode."""

    name: str
    editable: bool

    @property
    def label(self) -> str:
        """Return a human-readable label for the table."""
        return self.name.replace("_", " ").title()


@dataclass(frozen=True)
class AdminTableRow:
    """A redacted database row ready for the server-rendered table browser."""

    cells: list[str]
    edit_url: str | None


@dataclass(frozen=True)
class AdminTablePage:
    """One bounded page of a table-browser result."""

    table: AdminTableInfo
    columns: list[str]
    edit_column: str | None
    rows: list[AdminTableRow]
    page: int
    has_next: bool


class AdminReadRepository:
    """Aggregate and list reads across orders, invoices, disputes, and money."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    def list_table_catalog(self) -> list[AdminTableInfo]:
        """Return every application-owned table, excluding database extension tables."""
        return [
            AdminTableInfo(name=name, editable=name in _EDITABLE_TABLES)
            for name in sorted(Base.metadata.tables)
        ]

    async def list_table_page(self, table_name: str, *, page: int) -> AdminTablePage | None:
        """Return a bounded, redacted page for a known application table.

        The table name is resolved only from SQLAlchemy metadata, never interpolated into
        SQL. Sensitive values remain hidden even from this broad administrative browser;
        dedicated step-up flows are the sole path to reveal restricted identity/IBAN data.
        """
        table = Base.metadata.tables.get(table_name)
        if table is None:
            return None
        info = AdminTableInfo(name=table_name, editable=table_name in _EDITABLE_TABLES)
        columns = [column.name for column in table.columns]
        ordering = table.c.get("created_at")
        if ordering is None:
            ordering = next(iter(table.primary_key.columns), None)
        query = select(table)
        if ordering is not None:
            query = query.order_by(ordering.desc())
        result = await self._session.execute(
            query.limit(_PAGE_SIZE + 1).offset((page - 1) * _PAGE_SIZE)
        )
        mappings = list(result.mappings())
        has_next = len(mappings) > _PAGE_SIZE
        rows = [
            AdminTableRow(
                cells=[self._display_value(column, row[column]) for column in columns],
                edit_url=self._edit_url(table_name, row),
            )
            for row in mappings[:_PAGE_SIZE]
        ]
        return AdminTablePage(
            table=info,
            columns=columns,
            edit_column=_EDIT_URL_COLUMNS.get(table_name),
            rows=rows,
            page=page,
            has_next=has_next,
        )

    @staticmethod
    def _edit_url(table_name: str, row: Any) -> str | None:
        """Return the restricted edit URL for one of the explicitly editable tables."""
        key = _EDIT_URL_COLUMNS.get(table_name)
        if key is None or row[key] is None:
            return None
        plural = "couriers" if table_name == "courier_profiles" else table_name
        return f"/admin/{plural}/{row[key]}"

    @staticmethod
    def _display_value(column: str, value: object) -> str:
        """Format a DB value for display while redacting sensitive columns."""
        normalized = column.lower()
        if normalized in _REDACTED_COLUMNS or any(
            marker in normalized for marker in _REDACTED_MARKERS
        ):
            return "••••••"
        if value is None:
            return "—"
        if isinstance(value, Enum):
            return str(value.value)
        if isinstance(value, (date, datetime, uuid.UUID, Decimal)):
            return str(value)
        if isinstance(value, (dict, list)):
            return AdminReadRepository._truncate(json.dumps(value, default=str, ensure_ascii=False))
        return AdminReadRepository._truncate(str(value))

    @staticmethod
    def _truncate(value: str, limit: int = 160) -> str:
        """Keep browser cells compact without changing the underlying read query."""
        return value if len(value) <= limit else f"{value[: limit - 1]}…"

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
