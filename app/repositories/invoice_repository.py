"""Invoice and invoice-item persistence (SPEC SECTION 11, 14).

Every stored amount is the OUTPUT of the pricing engine; this layer never computes a
price, it only writes what ``core/pricing.py`` produced and reads it back verbatim.
Ownership on reads is enforced in the query (customer or courier of the parent order),
never fetch-then-compare.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pricing import PricingResult
from app.models import Invoice, InvoiceItem, Order
from app.models.enums import InvoiceStatus

# An invoice in one of these statuses blocks a second active invoice for the order
# (mirrors the partial unique index ``uq_invoices_one_active_per_order``).
_ACTIVE_STATUSES = (InvoiceStatus.DRAFT, InvoiceStatus.ISSUED, InvoiceStatus.PAID)


class InvoiceRepository:
    """Creates and reads invoices, with FOR UPDATE locking for pay/cancel."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def create_draft(
        self,
        *,
        order_id: uuid.UUID,
        courier_id: uuid.UUID,
        result: PricingResult,
        promo_id: uuid.UUID | None,
        promo_code_snapshot: str | None,
    ) -> Invoice:
        """Insert a DRAFT invoice from a computed pricing result.

        Items can only be attached while DRAFT (the ``enforce_invoice_item_freeze``
        trigger), so the invoice is created DRAFT, lined, then issued. The amounts are
        copied straight from ``result``; the DB CHECKs re-verify the arithmetic.
        """
        invoice = Invoice(
            order_id=order_id,
            issued_by_courier_id=courier_id,
            status=InvoiceStatus.DRAFT,
            items_net_amount=result.items_net_amount,
            courier_fee_amount=result.courier_fee_amount,
            service_fee_amount=result.service_fee_amount,
            discount_amount=result.discount_amount,
            net_after_discount_amount=result.net_after_discount_amount,
            tax_amount=result.tax_amount,
            total_amount=result.total_amount,
            promo_id=promo_id,
            promo_code_snapshot=promo_code_snapshot,
            pricing_breakdown=result.breakdown,
        )
        self._session.add(invoice)
        await self._session.flush()
        return invoice

    async def issue(self, invoice: Invoice, *, issued_at: datetime, expires_at: datetime) -> None:
        """Transition a fully-lined DRAFT invoice to ISSUED, stamping the deadlines."""
        invoice.status = InvoiceStatus.ISSUED
        invoice.issued_at = issued_at
        invoice.expires_at = expires_at
        await self._session.flush()

    async def add_item(self, *, invoice_id: uuid.UUID, line: object) -> None:
        """Attach one computed line (a :class:`PricingLine`) to an invoice."""
        # ``line`` is a PricingLine; typed as object to avoid a core->repo import cycle
        # in signatures. Attribute access is checked by the caller's typing.
        self._session.add(
            InvoiceItem(
                invoice_id=invoice_id,
                position=line.position,  # type: ignore[attr-defined]
                title=line.title,  # type: ignore[attr-defined]
                description=line.description,  # type: ignore[attr-defined]
                unit_price_amount=line.unit_price_amount,  # type: ignore[attr-defined]
                quantity=line.quantity,  # type: ignore[attr-defined]
                tax_rate=line.tax_rate,  # type: ignore[attr-defined]
                line_net_amount=line.line_net_amount,  # type: ignore[attr-defined]
                line_discount_amount=line.line_discount_amount,  # type: ignore[attr-defined]
                line_taxable_amount=line.line_taxable_amount,  # type: ignore[attr-defined]
                line_tax_amount=line.line_tax_amount,  # type: ignore[attr-defined]
                line_total_amount=line.line_total_amount,  # type: ignore[attr-defined]
            )
        )
        await self._session.flush()

    async def get(self, invoice_id: uuid.UUID) -> Invoice | None:
        """Return an invoice by id, or None."""
        return await self._session.get(Invoice, invoice_id)

    async def get_for_actor(self, invoice_id: uuid.UUID, actor_id: uuid.UUID) -> Invoice | None:
        """Return an invoice only if the actor is the order's customer or courier."""
        result: Invoice | None = await self._session.scalar(
            select(Invoice)
            .join(Order, Order.id == Invoice.order_id)
            .where(
                Invoice.id == invoice_id,
                (Order.customer_id == actor_id) | (Order.courier_id == actor_id),
            )
        )
        return result

    async def get_active_for_order(self, order_id: uuid.UUID) -> Invoice | None:
        """Return the order's active (DRAFT/ISSUED/PAID) invoice, or None."""
        result: Invoice | None = await self._session.scalar(
            select(Invoice).where(
                Invoice.order_id == order_id, Invoice.status.in_(_ACTIVE_STATUSES)
            )
        )
        return result

    async def get_active_for_order_for_actor(
        self, order_id: uuid.UUID, actor_id: uuid.UUID
    ) -> Invoice | None:
        """Return the order's active invoice only if the actor participates in the order."""
        result: Invoice | None = await self._session.scalar(
            select(Invoice)
            .join(Order, Order.id == Invoice.order_id)
            .where(
                Invoice.order_id == order_id,
                Invoice.status.in_(_ACTIVE_STATUSES),
                (Order.customer_id == actor_id) | (Order.courier_id == actor_id),
            )
        )
        return result

    async def lock(self, invoice_id: uuid.UUID) -> Invoice | None:
        """Load an invoice FOR UPDATE (pay/cancel serialization)."""
        result: Invoice | None = await self._session.scalar(
            select(Invoice).where(Invoice.id == invoice_id).with_for_update()
        )
        return result

    async def lock_for_courier(
        self, invoice_id: uuid.UUID, courier_id: uuid.UUID
    ) -> Invoice | None:
        """Load an invoice FOR UPDATE only if this courier issued it (ownership in query)."""
        result: Invoice | None = await self._session.scalar(
            select(Invoice)
            .where(Invoice.id == invoice_id, Invoice.issued_by_courier_id == courier_id)
            .with_for_update()
        )
        return result

    async def list_items(self, invoice_id: uuid.UUID) -> list[InvoiceItem]:
        """Return an invoice's items in render order."""
        return list(
            await self._session.scalars(
                select(InvoiceItem)
                .where(InvoiceItem.invoice_id == invoice_id)
                .order_by(InvoiceItem.position)
            )
        )

    async def flush(self) -> None:
        """Flush pending writes."""
        await self._session.flush()
