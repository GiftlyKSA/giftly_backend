"""The invoice-paid receipt (SPEC SECTION 5.3, 21).

The system sends exactly ONE email — the receipt for a paid invoice. Delivery is driven
by the ``idx_invoices_receipt_pending`` index (status='PAID' AND receipt_email_sent_at
IS NULL): a sweeper drains it, so a receipt is never lost even if a prior attempt failed.

Sending is at-most-once per pass under a row lock (a second sweeper blocks, then sees the
stamp and skips); a crash between send and commit yields an at-least-once retry, which for
a receipt is friendlier than losing it. Template variables carry only amounts and the
order reference — never a phone, coordinates, or any Restricted data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.money import money_str
from app.integrations.email.base import EmailClient
from app.models.enums import InvoiceStatus
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.user_repository import UserRepository

_DEFAULT_TEMPLATE_KEY = "invoice_paid_receipt"


class ReceiptService:
    """Sends the paid-invoice receipt to the customer, exactly once."""

    def __init__(
        self,
        *,
        invoices: InvoiceRepository,
        orders: OrderRepository,
        users: UserRepository,
        email: EmailClient,
        settings: Settings,
    ) -> None:
        """Wire the repositories and the email client."""
        self._invoices = invoices
        self._orders = orders
        self._users = users
        self._email = email
        self._settings = settings

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _template_key(self) -> str:
        return self._settings.SNDR_INVOICE_PAID_TEMPLATE_KEY or _DEFAULT_TEMPLATE_KEY

    async def send_receipt(self, invoice_id: uuid.UUID) -> bool:
        """Send one paid-invoice receipt, idempotently. Returns True if an email was sent.

        Skips (returns False) when the invoice is missing, not PAID, already receipted,
        or the customer has no email on file — in the no-email case the invoice is still
        stamped so it leaves the pending set and is not retried forever.
        """
        invoice = await self._invoices.lock(invoice_id)
        if (
            invoice is None
            or invoice.status is not InvoiceStatus.PAID
            or invoice.receipt_email_sent_at is not None
        ):
            return False

        order = await self._orders.get(invoice.order_id)
        customer = await self._users.get(order.customer_id) if order is not None else None
        email = customer.email if customer is not None else None
        if not email:
            # No address to send to — stamp so the sweeper stops retrying this invoice.
            await self._invoices.mark_receipt_sent(invoice, when=self._now())
            return False

        variables: dict[str, object] = {
            "invoice_id": str(invoice.id),
            "order_id": str(invoice.order_id),
            "currency": invoice.currency,
            "items_net_amount": money_str(invoice.items_net_amount),
            "courier_fee_amount": money_str(invoice.courier_fee_amount),
            "service_fee_amount": money_str(invoice.service_fee_amount),
            "discount_amount": money_str(invoice.discount_amount),
            "tax_amount": money_str(invoice.tax_amount),
            "total_amount": money_str(invoice.total_amount),
            "promo_code": invoice.promo_code_snapshot or "",
            "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else "",
        }
        # Send first, then stamp: a receipt is friendlier arriving twice than never.
        await self._email.send_transactional(email, self._template_key(), variables)
        await self._invoices.mark_receipt_sent(invoice, when=self._now())
        return True
