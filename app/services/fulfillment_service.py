"""Order fulfilment: delivery, approval, and disputes (SPEC SECTION 20.G-H).

The escrow lifecycle lives here. A courier submits delivery proof inside the geofence;
the customer (or the auto-approve job) approves, which RELEASES escrow through the money
service — the courier is paid on the pre-discount base, tax accrues, and the platform
keeps the residue. Either party may DISPUTE, freezing escrow until an admin resolves it.
Money only ever moves through the double-entry ledger; the actor id comes from the JWT.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationDomainError,
)
from app.core.money import ZERO, quantize_money
from app.core.pricing import compute_settlement
from app.models import Dispute, Invoice, Order
from app.models.enums import DisputeStatus, InvoiceStatus, MediaType, OrderStatus
from app.repositories.dispute_repository import DisputeRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.wallet_repository import WalletRepository
from app.services.media_service import MediaService
from app.services.money_service import MoneyService
from app.services.order_state import assert_transition


@dataclass(frozen=True)
class DeliveryInput:
    """A courier's delivery submission."""

    latitude: float
    longitude: float
    proof_media_keys: list[str]
    note: str | None


_MAX_PROOF_MEDIA = 5


class FulfillmentService:
    """Drives delivery, approval, escrow release, and disputes."""

    def __init__(
        self,
        *,
        orders: OrderRepository,
        invoices: InvoiceRepository,
        disputes: DisputeRepository,
        wallets: WalletRepository,
        money: MoneyService,
        media: MediaService,
        settings: Settings,
    ) -> None:
        """Wire the collaborators the fulfilment flows need."""
        self._orders = orders
        self._invoices = invoices
        self._disputes = disputes
        self._wallets = wallets
        self._money = money
        self._media = media
        self._settings = settings

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    async def submit_delivery(
        self, *, order_id: uuid.UUID, courier_id: uuid.UUID, data: DeliveryInput
    ) -> Order:
        """Mark an in-progress order DELIVERED with geofenced proof (assigned courier).

        Raises:
            NotFoundError: Not this courier's order.
            InvalidStateTransitionError: The order is not IN_PROGRESS.
            ValidationDomainError: Outside the delivery radius, missing/too many photos, or
                a proof object that fails validation.
        """
        if await self._orders.get_for_actor(order_id, courier_id) is None:
            raise NotFoundError("Order not found.")
        order = await self._orders.lock(order_id)
        if order is None:  # pragma: no cover - just confirmed it exists
            raise NotFoundError("Order not found.")
        assert_transition(order.status, OrderStatus.DELIVERED)

        if not data.proof_media_keys:
            raise ValidationDomainError("At least one delivery photo is required.")
        if len(data.proof_media_keys) > _MAX_PROOF_MEDIA:
            raise ValidationDomainError("At most 5 delivery photos are allowed.")
        distance = await self._orders.distance_to_delivery(
            order_id, longitude=data.longitude, latitude=data.latitude
        )
        if distance is None or distance > self._settings.MAX_DELIVERY_RADIUS_METERS:
            raise ValidationDomainError("You are too far from the drop-off location.")

        for key in data.proof_media_keys:
            await self._media.confirm(key)
        captured_at = self._now()
        for key in data.proof_media_keys:
            await self._orders.add_media(
                order_id=order.id,
                uploaded_by_user_id=courier_id,
                media_type=MediaType.DELIVERY_PROOF,
                storage_key=key,
                content_type="image/jpeg",
                byte_size=0,
                capture_longitude=data.longitude,
                capture_latitude=data.latitude,
                captured_at=captured_at,
            )
        order.status = OrderStatus.DELIVERED
        order.delivered_at = self._now()
        await self._orders.flush()
        return order

    async def approve_order(self, *, order_id: uuid.UUID, customer_id: uuid.UUID) -> Order:
        """Approve a delivered order (customer): complete it and release escrow.

        Raises:
            NotFoundError: Not this customer's order.
            InvalidStateTransitionError: The order is not DELIVERED.
        """
        if await self._orders.get_for_actor(order_id, customer_id) is None:
            raise NotFoundError("Order not found.")
        order = await self._orders.lock(order_id)
        if order is None:  # pragma: no cover - just confirmed it exists
            raise NotFoundError("Order not found.")
        await self._complete_and_release(order)
        return order

    async def auto_approve(self, *, order_id: uuid.UUID) -> bool:
        """Complete a still-DELIVERED order without a customer action (auto-approve job).

        Returns True if it was completed, False if it had already moved on (idempotent).
        """
        order = await self._orders.lock(order_id)
        if order is None or order.status is not OrderStatus.DELIVERED:
            return False
        await self._complete_and_release(order)
        return True

    async def _complete_and_release(self, order: Order) -> None:
        assert_transition(order.status, OrderStatus.COMPLETED)
        invoice = await self._invoices.get_active_for_order(order.id)
        if invoice is None or invoice.status is not InvoiceStatus.PAID:
            raise ConflictError("The order has no paid invoice to settle.")
        if order.courier_id is None:
            raise ConflictError("The order has no assigned courier.")
        courier_wallet = await self._wallets.get_by_user(order.courier_id)
        if courier_wallet is None:  # pragma: no cover - courier always has a wallet
            raise NotFoundError("Courier wallet not found.")

        settlement = compute_settlement(
            items_net_amount=invoice.items_net_amount,
            courier_fee_amount=invoice.courier_fee_amount,
            tax_amount=invoice.tax_amount,
            total_amount=invoice.total_amount,
            commission_rate=self._settings.PLATFORM_COMMISSION_RATE,
        )
        await self._money.release_escrow_on_completion(
            order_id=order.id,
            invoice_id=invoice.id,
            courier_wallet_id=courier_wallet.id,
            courier_payout_amount=settlement.courier_payout_amount,
            tax_amount=settlement.tax_amount,
            platform_revenue_amount=settlement.platform_revenue_amount,
        )
        order.status = OrderStatus.COMPLETED
        order.completed_at = self._now()
        order.commission_amount = settlement.commission_amount
        order.courier_payout_amount = settlement.courier_payout_amount
        await self._orders.flush()

    async def raise_dispute(
        self, *, order_id: uuid.UUID, actor_id: uuid.UUID, reason: str
    ) -> Dispute:
        """Open a dispute on an in-progress or delivered order (either participant).

        Raises:
            NotFoundError: Not a participant's order.
            InvalidStateTransitionError: The order cannot be disputed from its state.
            ConflictError: A dispute already exists for this order.
        """
        if await self._orders.get_for_actor(order_id, actor_id) is None:
            raise NotFoundError("Order not found.")
        order = await self._orders.lock(order_id)
        if order is None:  # pragma: no cover
            raise NotFoundError("Order not found.")
        if await self._disputes.get_for_order(order.id) is not None:
            raise ConflictError("A dispute already exists for this order.")
        assert_transition(order.status, OrderStatus.DISPUTED)
        order.status = OrderStatus.DISPUTED
        dispute = await self._disputes.create(
            order_id=order.id, raised_by_user_id=actor_id, reason=reason
        )
        await self._orders.flush()
        return dispute

    async def resolve_dispute(
        self,
        *,
        dispute_id: uuid.UUID,
        admin_id: uuid.UUID,
        outcome: DisputeStatus,
        note: str | None,
        courier_amount: Decimal | None = None,
    ) -> Dispute:
        """Resolve a dispute, moving escrow accordingly (admin only).

        ``RESOLVED_CUSTOMER`` refunds the full total; ``RESOLVED_COURIER`` releases the
        normal completion payout; ``RESOLVED_SPLIT`` divides the total between courier and
        customer by ``courier_amount`` (the platform books nothing on a split).

        Raises:
            NotFoundError: No such dispute.
            ConflictError: The dispute is already resolved.
            ValidationDomainError: A bad outcome or split amount.
        """
        dispute = await self._disputes.lock(dispute_id)
        if dispute is None:
            raise NotFoundError("Dispute not found.")
        if dispute.status is not DisputeStatus.OPEN:
            raise ConflictError("This dispute is already resolved.")
        order = await self._orders.lock(dispute.order_id)
        if order is None:  # pragma: no cover - FK guarantees the order
            raise NotFoundError("Order not found.")

        invoice = await self._invoices.get_active_for_order(order.id)
        if invoice is None or invoice.status is not InvoiceStatus.PAID:
            raise ConflictError("The order has no paid invoice to settle.")

        target = await self._apply_dispute_outcome(order, invoice, outcome, courier_amount)
        assert_transition(order.status, target)
        order.status = target
        if target is OrderStatus.COMPLETED:
            order.completed_at = self._now()
        await self._disputes.resolve(
            dispute, status=outcome, admin_id=admin_id, note=note, when=self._now()
        )
        await self._orders.flush()
        return dispute

    async def _apply_dispute_outcome(
        self,
        order: Order,
        invoice: Invoice,
        outcome: DisputeStatus,
        courier_amount: Decimal | None,
    ) -> OrderStatus:
        """Move escrow for a dispute outcome and return the order's target status."""
        if order.courier_id is None:
            raise ConflictError("The order has no assigned courier.")
        courier_wallet = await self._wallets.get_by_user(order.courier_id)
        customer_wallet = await self._wallets.get_by_user(order.customer_id)
        if courier_wallet is None or customer_wallet is None:  # pragma: no cover
            raise NotFoundError("A wallet was not found.")
        total = invoice.total_amount

        if outcome is DisputeStatus.RESOLVED_CUSTOMER:
            await self._money.refund_escrow(
                order_id=order.id,
                invoice_id=invoice.id,
                customer_wallet_id=customer_wallet.id,
                amount=total,
            )
            return OrderStatus.REFUNDED
        if outcome is DisputeStatus.RESOLVED_COURIER:
            settlement = compute_settlement(
                items_net_amount=invoice.items_net_amount,
                courier_fee_amount=invoice.courier_fee_amount,
                tax_amount=invoice.tax_amount,
                total_amount=total,
                commission_rate=self._settings.PLATFORM_COMMISSION_RATE,
            )
            await self._money.release_escrow_on_completion(
                order_id=order.id,
                invoice_id=invoice.id,
                courier_wallet_id=courier_wallet.id,
                courier_payout_amount=settlement.courier_payout_amount,
                tax_amount=settlement.tax_amount,
                platform_revenue_amount=settlement.platform_revenue_amount,
            )
            order.commission_amount = settlement.commission_amount
            order.courier_payout_amount = settlement.courier_payout_amount
            return OrderStatus.COMPLETED
        if outcome is DisputeStatus.RESOLVED_SPLIT:
            if courier_amount is None:
                raise ValidationDomainError("A split resolution requires a courier amount.")
            courier_share = quantize_money(courier_amount)
            if courier_share < ZERO or courier_share > quantize_money(total):
                raise ValidationDomainError("The courier amount must be between 0 and the total.")
            await self._money.split_escrow(
                order_id=order.id,
                invoice_id=invoice.id,
                courier_wallet_id=courier_wallet.id,
                customer_wallet_id=customer_wallet.id,
                courier_amount=courier_share,
                refund_amount=total - courier_share,
            )
            order.courier_payout_amount = courier_share
            return OrderStatus.COMPLETED
        raise ValidationDomainError("Unsupported dispute outcome.")

    def auto_approve_cutoff(self) -> datetime:
        """The delivered-before timestamp at which an order auto-approves."""
        return self._now() - timedelta(hours=self._settings.AUTO_APPROVE_HOURS)
