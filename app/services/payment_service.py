"""Payment orchestration: top-ups, invoice payment, and the gateway webhook.

There are exactly two reasons to call the gateway — a wallet top-up and an invoice
remainder — both unified through ``payment_intents`` (ADR 0003). The webhook verifies
the HMAC over the RAW body, does one lookup by StreamPay payment-link ID, and dispatches on
``purpose``. Settlement is idempotent at three layers: a Redis lock on the transaction
payment-link ID, the intent's own status check, and the ledger's idempotency keys.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    InvalidStateTransitionError,
    InvalidWebhookSignatureError,
    NotFoundError,
    PaymentAmountMismatchError,
    ValidationDomainError,
)
from app.core.locks import redis_lock
from app.core.money import ZERO, parse_money, quantize_money
from app.integrations.streampay.base import (
    StreamPayCheckout,
    StreamPayClient,
    StreamPayCustomer,
    StreamPayItem,
)
from app.models import Invoice, InvoiceItem, Order, PaymentIntent, User
from app.models.enums import (
    InvoiceStatus,
    OrderStatus,
    PaymentIntentStatus,
    PaymentMethod,
    PaymentPurpose,
)
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.payment_repository import PaymentRepository
from app.repositories.promo_repository import PromoRepository
from app.repositories.user_repository import UserRepository
from app.repositories.wallet_repository import WalletRepository
from app.services.money_service import MoneyService
from app.services.order_state import assert_transition
from app.services.promo_service import PromoService

_WEBHOOK_LOCK_TTL = 15


@dataclass(frozen=True)
class TopupResult:
    """The outcome of starting a wallet top-up."""

    intent_id: uuid.UUID
    amount: Decimal
    payment_url: str


@dataclass(frozen=True)
class PayResult:
    """The outcome of paying an invoice."""

    invoice_id: uuid.UUID
    status: str  # "PAID" (settled from wallet) or "PENDING" (awaiting the gateway)
    amount_from_wallet: Decimal
    amount_from_gateway: Decimal
    payment_url: str | None


@dataclass(frozen=True)
class WebhookEvent:
    """A parsed gateway webhook payload."""

    payment_link_id: str
    status: str
    amount: Decimal | None


@dataclass(frozen=True)
class WebhookResult:
    """The outcome of processing a webhook."""

    outcome: str  # "processed" | "already_processed" | "failed"


class PaymentService:
    """Starts gateway payments and settles them on the webhook."""

    def __init__(
        self,
        *,
        payments: PaymentRepository,
        invoices: InvoiceRepository,
        orders: OrderRepository,
        wallets: WalletRepository,
        money: MoneyService,
        promos: PromoService,
        users: UserRepository,
        gateway: StreamPayClient,
        redis: Redis,
        settings: Settings,
    ) -> None:
        """Wire the collaborators the payment flows need."""
        self._payments = payments
        self._invoices = invoices
        self._orders = orders
        self._wallets = wallets
        self._money = money
        self._promos = promos
        self._users = users
        self._gateway = gateway
        self._redis = redis
        self._settings = settings

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _expiry(self) -> datetime:
        return self._now() + timedelta(hours=self._settings.PAYMENT_EXPIRY_HOURS)

    async def create_topup(self, *, user_id: uuid.UUID, amount: Decimal) -> TopupResult:
        """Start a wallet top-up: create the intent and a StreamPay payment link.

        Raises:
            ValidationDomainError: The amount is outside the permitted top-up bounds.
            NotFoundError: The user has no wallet.
        """
        amount = quantize_money(amount)
        if not (self._settings.MIN_TOPUP_AMOUNT <= amount <= self._settings.MAX_TOPUP_AMOUNT):
            raise ValidationDomainError(
                f"Top-up must be between {self._settings.MIN_TOPUP_AMOUNT} and "
                f"{self._settings.MAX_TOPUP_AMOUNT}."
            )
        wallet = await self._wallets.get_by_user(user_id)
        if wallet is None:
            raise NotFoundError("Wallet not found.")

        intent = await self._payments.create_intent(
            user_id=user_id,
            purpose=PaymentPurpose.WALLET_TOPUP,
            amount=amount,
            reference_invoice_id=None,
            expires_at=self._expiry(),
        )
        await self._payments.create_topup(
            user_id=user_id, wallet_id=wallet.id, payment_intent_id=intent.id, amount=amount
        )
        checkout = await self._create_checkout(
            intent=intent,
            user_id=user_id,
            items=(
                StreamPayItem(
                    name="SAFE-GIFT wallet top-up",
                    description="Wallet credit",
                    amount=amount,
                ),
            ),
        )
        await self._payments.attach_streampay(
            intent, payment_link_id=checkout.payment_link_id, url=checkout.payment_url
        )
        return TopupResult(intent_id=intent.id, amount=amount, payment_url=checkout.payment_url)

    async def pay_invoice(self, *, invoice_id: uuid.UUID, customer_id: uuid.UUID) -> PayResult:
        """Pay an issued invoice from wallet, gateway, or a split of both.

        If the wallet fully covers the total, the payment settles synchronously into
        escrow and the order moves to IN_PROGRESS. Otherwise the wallet portion is held
        and a StreamPay payment link is created for the remainder; the webhook settles it.

        Raises:
            NotFoundError: No such invoice for this customer.
            ConflictError: The invoice is not payable (already paid/cancelled/expired).
            InvalidStateTransitionError: The order is not awaiting payment.
            InsufficientFundsError: A concurrent debit consumed the held balance.
        """
        invoice = await self._invoices.get_for_actor(invoice_id, customer_id)
        if invoice is None:
            raise NotFoundError("Invoice not found.")
        if invoice.status is not InvoiceStatus.ISSUED:
            raise ConflictError("This invoice is not awaiting payment.")
        if invoice.expires_at is not None and self._now() > invoice.expires_at:
            raise ConflictError("This invoice has expired.")

        order = await self._orders.lock(invoice.order_id)
        if order is None:  # pragma: no cover - FK guarantees the order exists
            raise NotFoundError("Order not found.")
        if order.status is not OrderStatus.WAITING_PAYMENT:
            raise InvalidStateTransitionError("This order is not awaiting payment.")

        wallet = await self._wallets.get_by_user(customer_id)
        if wallet is None:  # pragma: no cover - the customer always has a wallet
            raise NotFoundError("Wallet not found.")

        total = quantize_money(invoice.total_amount)
        available = await self._money.available_balance(customer_id)
        wallet_amount = min(available, total)
        gateway_amount = quantize_money(total - wallet_amount)

        if gateway_amount <= ZERO:
            # Wallet fully covers the total — settle immediately, no gateway round-trip.
            await self._money.fund_escrow_for_invoice(
                customer_wallet_id=wallet.id,
                wallet_amount=total,
                gateway_amount=ZERO,
                invoice_id=invoice.id,
                order_id=order.id,
                intent_id=None,
                was_held=False,
            )
            await self._settle_invoice_record(
                invoice, order, PaymentMethod.WALLET_ONLY, total, ZERO
            )
            return PayResult(
                invoice_id=invoice.id,
                status="PAID",
                amount_from_wallet=total,
                amount_from_gateway=ZERO,
                payment_url=None,
            )

        # A remainder is due from the gateway. Reuse an open intent if one exists.
        existing = await self._payments.get_open_intent_for_invoice(invoice.id)
        if existing is not None and existing.streampay_payment_url is not None:
            return PayResult(
                invoice_id=invoice.id,
                status="PENDING",
                amount_from_wallet=invoice.amount_from_wallet,
                amount_from_gateway=existing.amount,
                payment_url=existing.streampay_payment_url,
            )

        method = PaymentMethod.SPLIT if wallet_amount > ZERO else PaymentMethod.GATEWAY_ONLY
        if wallet_amount > ZERO:
            # Reserve the wallet portion so it cannot back a second pending payment.
            await self._money.hold_funds(wallet_id=wallet.id, amount=wallet_amount)
        invoice.amount_from_wallet = wallet_amount
        invoice.amount_from_gateway = gateway_amount
        invoice.payment_method = method

        intent = await self._payments.create_intent(
            user_id=customer_id,
            purpose=PaymentPurpose.ORDER_INVOICE,
            amount=gateway_amount,
            reference_invoice_id=invoice.id,
            expires_at=self._expiry(),
        )
        checkout = await self._create_checkout(
            intent=intent,
            user_id=customer_id,
            items=self._invoice_checkout_items(
                invoice_id=invoice.id,
                payment_amount=gateway_amount,
                invoice_items=await self._invoices.list_items(invoice.id),
            ),
        )
        await self._payments.attach_streampay(
            intent, payment_link_id=checkout.payment_link_id, url=checkout.payment_url
        )
        await self._invoices.flush()
        return PayResult(
            invoice_id=invoice.id,
            status="PENDING",
            amount_from_wallet=wallet_amount,
            amount_from_gateway=gateway_amount,
            payment_url=checkout.payment_url,
        )

    async def handle_webhook(self, *, raw_body: bytes, signature: str) -> WebhookResult:
        """Verify and process a gateway webhook.

        The signature is verified over the RAW body (never a re-serialized dict). A Redis
        lock on the StreamPay payment-link ID serializes concurrent duplicate deliveries.

        Raises:
            InvalidWebhookSignatureError: The HMAC does not match.
            NotFoundError: No intent matches the StreamPay payment-link ID.
            PaymentAmountMismatchError: The webhook amount != the intent amount.
        """
        if not self._gateway.verify_webhook_signature(raw_body, signature):
            raise InvalidWebhookSignatureError()
        event = self._parse(raw_body)
        async with redis_lock(
            self._redis, f"lock:webhook:{event.payment_link_id}", ttl_seconds=_WEBHOOK_LOCK_TTL
        ):
            return await self._settle_locked(event)

    async def _settle_locked(self, event: WebhookEvent) -> WebhookResult:
        intent = await self._payments.lock_intent_by_payment_link(event.payment_link_id)
        if intent is None:
            raise NotFoundError("Unknown StreamPay payment link.")
        if intent.status is not PaymentIntentStatus.NEW:
            # Already PAID/FAILED — a replay. Idempotent no-op.
            return WebhookResult(outcome="already_processed")
        if event.status.upper() != "PAID":
            await self._payments.mark_failed(intent, reason=event.status)
            return WebhookResult(outcome="failed")
        if event.amount is None or quantize_money(event.amount) != quantize_money(intent.amount):
            raise PaymentAmountMismatchError()

        if intent.purpose is PaymentPurpose.WALLET_TOPUP:
            await self._settle_topup(intent)
        else:
            await self._settle_invoice(intent)
        await self._payments.mark_paid(intent, paid_at=self._now())
        return WebhookResult(outcome="processed")

    async def _settle_topup(self, intent: PaymentIntent) -> None:
        wallet = await self._wallets.get_by_user(intent.user_id)
        if wallet is None:  # pragma: no cover - the top-up user always has a wallet
            raise NotFoundError("Wallet not found.")
        await self._money.credit_topup(
            user_wallet_id=wallet.id, amount=intent.amount, intent_id=intent.id
        )

    async def _settle_invoice(self, intent: PaymentIntent) -> None:
        if intent.reference_invoice_id is None:
            raise ConflictError("The payment intent has no invoice reference.")
        invoice = await self._invoices.lock(intent.reference_invoice_id)
        if invoice is None:  # pragma: no cover - FK guarantees the invoice exists
            raise NotFoundError("Invoice not found.")
        if invoice.status is InvoiceStatus.PAID:  # pragma: no cover - intent guard precedes
            return
        order = await self._orders.lock(invoice.order_id)
        if order is None:  # pragma: no cover - FK guarantees the order exists
            raise NotFoundError("Order not found.")
        wallet = await self._wallets.get_by_user(intent.user_id)
        if wallet is None:  # pragma: no cover
            raise NotFoundError("Wallet not found.")

        await self._money.fund_escrow_for_invoice(
            customer_wallet_id=wallet.id,
            wallet_amount=invoice.amount_from_wallet,
            gateway_amount=intent.amount,
            invoice_id=invoice.id,
            order_id=order.id,
            intent_id=intent.id,
            was_held=invoice.amount_from_wallet > ZERO,
        )
        method = invoice.payment_method or PaymentMethod.GATEWAY_ONLY
        await self._settle_invoice_record(
            invoice, order, method, invoice.amount_from_wallet, intent.amount
        )

    async def _settle_invoice_record(
        self,
        invoice: Invoice,
        order: Order,
        method: PaymentMethod,
        wallet_amount: Decimal,
        gateway_amount: Decimal,
    ) -> None:
        """Mark the invoice PAID, advance the order, and consume the promo."""
        now = self._now()
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = now
        invoice.payment_method = method
        invoice.amount_from_wallet = quantize_money(wallet_amount)
        invoice.amount_from_gateway = quantize_money(gateway_amount)
        assert_transition(order.status, OrderStatus.IN_PROGRESS)
        order.status = OrderStatus.IN_PROGRESS
        await self._promos.consume(invoice_id=invoice.id)
        await self._invoices.flush()

    async def _create_checkout(
        self, *, intent: PaymentIntent, user_id: uuid.UUID, items: tuple[StreamPayItem, ...]
    ) -> StreamPayCheckout:
        """Create a single-use hosted checkout for a known local customer."""
        user = await self._users.get(user_id)
        if user is None:  # pragma: no cover - intent FK guarantees the user exists
            raise NotFoundError("User not found.")
        return await self._gateway.create_payment_link(
            reference=str(intent.id),
            customer=self._streampay_customer(user),
            items=items,
            success_redirect_url=self._settings.STREAMPAY_SUCCESS_REDIRECT_URL,
            failure_redirect_url=self._settings.STREAMPAY_FAILURE_REDIRECT_URL,
        )

    @staticmethod
    def _streampay_customer(user: User) -> StreamPayCustomer:
        """Map the minimum local customer identity StreamPay needs for hosted checkout."""
        return StreamPayCustomer(
            external_id=str(user.id),
            name=user.full_name or "SAFE-GIFT customer",
            phone_number=user.phone,
            email=user.email,
        )

    @staticmethod
    def _invoice_checkout_items(
        *, invoice_id: uuid.UUID, payment_amount: Decimal, invoice_items: list[InvoiceItem]
    ) -> tuple[StreamPayItem, ...]:
        """Represent a payable invoice as Stream products whose sum equals the remainder.

        When the full invoice is paid externally, frozen invoice lines are sent one-for-one
        plus a visible invoice adjustment for delivery, fees, tax, and discounts. A split
        payment may be smaller than the item total, so it uses one authoritative balance
        line to ensure StreamPay's invoice matches the amount held in our ledger exactly.
        """
        source_total = sum((quantize_money(item.line_total_amount) for item in invoice_items), ZERO)
        if not invoice_items or source_total > payment_amount or any(
            item.line_total_amount < Decimal("1.00") for item in invoice_items
        ):
            return (
                StreamPayItem(
                    name=f"SAFE-GIFT invoice {invoice_id}",
                    description="Outstanding invoice balance",
                    amount=payment_amount,
                ),
            )

        items = [
            StreamPayItem(
                name=item.title,
                description=(
                    f"Quantity: {item.quantity}. {item.description or ''}".strip()
                ),
                amount=quantize_money(item.line_total_amount),
            )
            for item in invoice_items
        ]
        adjustment = quantize_money(payment_amount - source_total)
        if adjustment >= Decimal("1.00"):
            items.append(
                StreamPayItem(
                    name="Invoice adjustment",
                    description="Delivery, service fees, tax, and discounts",
                    amount=adjustment,
                )
            )
        elif adjustment > ZERO:
            last = items[-1]
            items[-1] = StreamPayItem(
                name=last.name,
                description=last.description,
                amount=quantize_money(last.amount + adjustment),
            )
        return tuple(items)

    @staticmethod
    def _parse(raw_body: bytes) -> WebhookEvent:
        try:
            data = json.loads(raw_body)
            details = data.get("data", {})
            if not isinstance(details, dict):
                raise TypeError("data must be an object")
            payment_link = details.get("payment_link", {})
            payment = details.get("payment", {})
            invoice = details.get("invoice", {})
            if not isinstance(payment_link, dict) or not isinstance(payment, dict):
                raise TypeError("payment data must be an object")
            amount = payment.get(
                "amount", invoice.get("amount") if isinstance(invoice, dict) else None
            )
            event_type = str(data.get("event_type", ""))
            return WebhookEvent(
                payment_link_id=str(payment_link["id"]),
                status=(
                    "PAID" if event_type.upper() == "PAYMENT_SUCCEEDED" else str(payment["status"])
                ),
                amount=parse_money(str(amount)) if amount is not None else None,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationDomainError("Malformed webhook payload.") from exc


def build_payment_service(
    *, session: AsyncSession, gateway: StreamPayClient, redis: Redis, settings: Settings
) -> PaymentService:
    """Assemble a PaymentService with fresh repositories bound to one session."""
    return PaymentService(
        payments=PaymentRepository(session),
        invoices=InvoiceRepository(session),
        orders=OrderRepository(session),
        wallets=WalletRepository(session),
        users=UserRepository(session),
        money=MoneyService(WalletRepository(session)),
        promos=PromoService(PromoRepository(session)),
        gateway=gateway,
        redis=redis,
        settings=settings,
    )
