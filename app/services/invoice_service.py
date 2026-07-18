"""Invoice authoring and lifecycle service (SPEC SECTION 11, 14).

The courier authors an itemised invoice; the platform computes the service fee, the
discount allocation, and all tax through the single pricing engine (``core/pricing.py``).
The client never sends the service fee, discount, tax, or total. Every stored amount is
the engine's output, frozen once issued; reads never recompute, so a later VAT or
service-fee change never restates a historical invoice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationDomainError,
)
from app.core.money import ZERO, quantize_money
from app.core.pricing import (
    PricingConfig,
    PricingItem,
    PricingPromo,
    calculate_invoice_totals,
)
from app.models import Invoice, InvoiceItem
from app.models.enums import InvoiceStatus, OrderStatus
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.services.order_state import assert_transition
from app.services.promo_service import PromoService, to_pricing_promo


@dataclass(frozen=True)
class InvoiceLineInput:
    """One line the courier entered, before any platform computation."""

    title: str
    unit_price_amount: Decimal
    quantity: int
    tax_rate: Decimal
    description: str | None = None


@dataclass(frozen=True)
class NewInvoiceInput:
    """Validated inputs for authoring an invoice."""

    items: list[InvoiceLineInput]
    courier_fee_amount: Decimal
    promo_code: str | None


@dataclass(frozen=True)
class PromoPreview:
    """A previewed promo application against an order's active invoice."""

    code: str
    discount_amount: Decimal
    original_total_amount: Decimal
    total_amount: Decimal


class InvoiceService:
    """Authors invoices, previews promos, and manages the invoice lifecycle."""

    def __init__(
        self,
        *,
        invoices: InvoiceRepository,
        orders: OrderRepository,
        promos: PromoService,
        settings: Settings,
    ) -> None:
        """Wire the collaborators the invoice flows need."""
        self._invoices = invoices
        self._orders = orders
        self._promos = promos
        self._settings = settings

    def _pricing_config(self) -> PricingConfig:
        s = self._settings
        return PricingConfig(
            service_fee_rate=s.SERVICE_FEE_RATE,
            service_fee_min_amount=s.SERVICE_FEE_MIN_AMOUNT,
            service_fee_max_amount=s.SERVICE_FEE_MAX_AMOUNT,
            default_vat_rate=s.DEFAULT_VAT_RATE,
            max_invoice_amount=s.MAX_INVOICE_AMOUNT,
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    async def create_invoice(
        self, *, order_id: uuid.UUID, courier_id: uuid.UUID, data: NewInvoiceInput
    ) -> Invoice:
        """Author and issue an invoice for an order, moving it to WAITING_PAYMENT.

        The assigned courier supplies items and a courier fee (both net of tax) and,
        optionally, the customer's promo code. The pricing engine computes every other
        leg; the promo (if any) is reserved against the customer.

        Raises:
            NotFoundError: The order does not exist for this courier (no existence leak).
            ValidationDomainError: Empty/oversized item list, or a promo that yields no
                discount for this invoice.
            InvalidStateTransitionError: The order is not in a state that can be invoiced.
            ConflictError: The order already has an active invoice.
            Promo* errors: The supplied promo failed validation (each a 422).
        """
        # Ownership is in the query: get_for_actor returns the order only if this courier
        # is its assignee (a non-participant courier gets 404, no existence leak).
        order = await self._orders.get_for_actor(order_id, courier_id)
        if order is None:
            raise NotFoundError("Order not found.")
        # ASSIGNED -> WAITING_PAYMENT is the only legal path into an invoice.
        assert_transition(order.status, OrderStatus.WAITING_PAYMENT)
        if await self._invoices.get_active_for_order(order_id) is not None:
            raise ConflictError("This order already has an active invoice.")

        self._validate_items(data.items)
        pricing_items = [
            PricingItem(
                title=line.title,
                unit_price_amount=line.unit_price_amount,
                quantity=line.quantity,
                tax_rate=line.tax_rate,
                description=line.description,
                position=idx + 1,
            )
            for idx, line in enumerate(data.items)
        ]

        promo_obj = None
        pricing_promo: PricingPromo | None = None
        if data.promo_code:
            base = self._discountable_base(pricing_items, data.courier_fee_amount)
            validation = await self._promos.validate(
                code=data.promo_code,
                discountable_base=base,
                user_id=order.customer_id,
            )
            promo_obj = validation.promo
            pricing_promo = to_pricing_promo(promo_obj)

        result = calculate_invoice_totals(
            pricing_items, data.courier_fee_amount, pricing_promo, self._pricing_config()
        )
        # The promo/discount pairing invariant (DB CHECK) requires a promo to actually
        # move the price; a code that rounds to a 0.00 discount is rejected, not stored.
        if promo_obj is not None and result.discount_amount <= ZERO:
            raise ValidationDomainError("This promo yields no discount for this invoice.")

        # Create DRAFT, attach items (only DRAFT permits item writes), then issue.
        invoice = await self._invoices.create_draft(
            order_id=order_id,
            courier_id=courier_id,
            result=result,
            promo_id=promo_obj.id if promo_obj is not None else None,
            promo_code_snapshot=promo_obj.code if promo_obj is not None else None,
        )
        for line in result.lines:
            await self._invoices.add_item(invoice_id=invoice.id, line=line)

        if promo_obj is not None:
            await self._promos.reserve(
                promo=promo_obj,
                user_id=order.customer_id,
                invoice_id=invoice.id,
                order_id=order_id,
                discount_amount=result.discount_amount,
            )

        now = self._now()
        expires_at = now + timedelta(hours=self._settings.PAYMENT_EXPIRY_HOURS)
        await self._invoices.issue(invoice, issued_at=now, expires_at=expires_at)

        order.status = OrderStatus.WAITING_PAYMENT
        order.total_amount = result.total_amount
        await self._invoices.flush()
        return invoice

    async def cancel_invoice(self, *, invoice_id: uuid.UUID, courier_id: uuid.UUID) -> Invoice:
        """Cancel an unpaid ISSUED invoice, releasing its promo and reopening the order.

        Lets the assigned courier correct a mistake: the order returns to ASSIGNED so a
        fresh invoice can be authored. The cancelled invoice row is never mutated further
        (immutability), only its status moves to CANCELLED.

        Raises:
            NotFoundError: No such invoice issued by this courier (no existence leak).
            InvalidStateTransitionError: The invoice is not ISSUED (already paid/cancelled).
        """
        invoice = await self._invoices.lock_for_courier(invoice_id, courier_id)
        if invoice is None:
            raise NotFoundError("Invoice not found.")
        if invoice.status is not InvoiceStatus.ISSUED:
            raise InvalidStateTransitionError("Only an issued, unpaid invoice can be cancelled.")

        order = await self._orders.lock(invoice.order_id)
        if order is None:  # pragma: no cover - FK guarantees the order exists
            raise NotFoundError("Order not found.")

        await self._promos.release(invoice_id=invoice.id)
        invoice.status = InvoiceStatus.CANCELLED
        # WAITING_PAYMENT -> ASSIGNED reopens authoring; if the order already moved on
        # (e.g. paid), the transition guard rejects the cancel.
        assert_transition(order.status, OrderStatus.ASSIGNED)
        order.status = OrderStatus.ASSIGNED
        order.total_amount = ZERO
        await self._invoices.flush()
        return invoice

    async def get_invoice_for_actor(
        self, *, invoice_id: uuid.UUID, actor_id: uuid.UUID
    ) -> tuple[Invoice, list[InvoiceItem]]:
        """Return an invoice (and its items) the actor participates in, else 404."""
        invoice = await self._invoices.get_for_actor(invoice_id, actor_id)
        if invoice is None:
            raise NotFoundError("Invoice not found.")
        items = await self._invoices.list_items(invoice.id)
        return invoice, items

    async def get_active_invoice_for_order(
        self, *, order_id: uuid.UUID, actor_id: uuid.UUID
    ) -> tuple[Invoice, list[InvoiceItem]]:
        """Return an order's active invoice (and items) for a participant, else 404."""
        invoice = await self._invoices.get_active_for_order_for_actor(order_id, actor_id)
        if invoice is None:
            raise NotFoundError("No active invoice for this order.")
        items = await self._invoices.list_items(invoice.id)
        return invoice, items

    async def preview_promo(
        self, *, order_id: uuid.UUID, code: str, customer_id: uuid.UUID
    ) -> PromoPreview:
        """Preview a promo against an order's active invoice, reserving nothing.

        Re-runs the pricing engine over the invoice's stored lines with the candidate
        promo so the customer sees the exact discount and resulting total before the
        courier re-issues with the code.

        Raises:
            NotFoundError: The order (for this customer) has no active invoice.
            Promo* errors: The promo failed validation (each a 422).
        """
        invoice = await self._invoices.get_active_for_order_for_actor(order_id, customer_id)
        if invoice is None:
            raise NotFoundError("No active invoice for this order.")
        items = await self._invoices.list_items(invoice.id)
        pricing_items = [
            PricingItem(
                title=item.title,
                unit_price_amount=item.unit_price_amount,
                quantity=item.quantity,
                tax_rate=item.tax_rate,
                description=item.description,
                position=item.position,
            )
            for item in items
        ]
        base = self._discountable_base(pricing_items, invoice.courier_fee_amount)
        validation = await self._promos.validate(
            code=code, discountable_base=base, user_id=customer_id
        )
        result = calculate_invoice_totals(
            pricing_items,
            invoice.courier_fee_amount,
            to_pricing_promo(validation.promo),
            self._pricing_config(),
        )
        return PromoPreview(
            code=validation.promo.code,
            discount_amount=result.discount_amount,
            original_total_amount=invoice.total_amount,
            total_amount=result.total_amount,
        )

    def _validate_items(self, items: list[InvoiceLineInput]) -> None:
        if not items:
            raise ValidationDomainError("An invoice must have at least one line.")
        if len(items) > self._settings.MAX_INVOICE_ITEMS:
            raise ValidationDomainError(
                f"An invoice may have at most {self._settings.MAX_INVOICE_ITEMS} lines."
            )
        for line in items:
            if line.unit_price_amount <= ZERO:
                raise ValidationDomainError("Each line unit price must be positive.")
            if line.unit_price_amount > self._settings.MAX_ITEM_UNIT_PRICE:
                raise ValidationDomainError("A line unit price exceeds the maximum permitted.")
            if not 1 <= line.quantity <= 999:
                raise ValidationDomainError("Each line quantity must be between 1 and 999.")
            if not ZERO <= line.tax_rate <= Decimal(1):
                raise ValidationDomainError("Each line tax rate must be between 0 and 1.")

    @staticmethod
    def _discountable_base(items: list[PricingItem], courier_fee_amount: Decimal) -> Decimal:
        """The base a promo discounts: item nets + courier fee (service fee excluded)."""
        items_net = sum((quantize_money(i.unit_price_amount * i.quantity) for i in items), ZERO)
        return items_net + quantize_money(courier_fee_amount)
