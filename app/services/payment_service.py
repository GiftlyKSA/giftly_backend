"""Payment orchestration: top-ups, invoice payment, and the gateway webhook.

There are exactly two reasons to call the gateway — a wallet top-up and an invoice
remainder — both unified through ``payment_intents`` (ADR 0003). The webhook verifies
the HMAC over the RAW body, does one lookup by transaction number, and dispatches on
``purpose``. Settlement is idempotent at three layers: a Redis lock on the transaction
number, the intent's own status check, and the ledger's idempotency keys.
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
from app.integrations.paylink.base import PaymentGateway
from app.models import Invoice, Order, PaymentIntent
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

    transaction_no: str
    status: str
    amount: Decimal


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
        gateway: PaymentGateway,
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
        self._gateway = gateway
        self._redis = redis
        self._settings = settings

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _expiry(self) -> datetime:
        return self._now() + timedelta(hours=self._settings.PAYMENT_EXPIRY_HOURS)

    def _callback_url(self) -> str:
        return self._settings.PAYLINK_CALLBACK_URL or "http://localhost:8000/api/webhooks/paylink"

    async def create_topup(self, *, user_id: uuid.UUID, amount: Decimal) -> TopupResult:
        """Start a wallet top-up: create the intent and a gateway charge.

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
        charge = await self._gateway.create_charge(
            amount=amount, order_number=str(intent.id), callback_url=self._callback_url()
        )
        await self._payments.attach_gateway(
            intent, transaction_no=charge.transaction_no, url=charge.payment_url
        )
        return TopupResult(intent_id=intent.id, amount=amount, payment_url=charge.payment_url)

    async def pay_invoice(self, *, invoice_id: uuid.UUID, customer_id: uuid.UUID) -> PayResult:
        """Pay an issued invoice from wallet, gateway, or a split of both.

        If the wallet fully covers the total, the payment settles synchronously into
        escrow and the order moves to IN_PROGRESS. Otherwise the wallet portion is held
        and a gateway charge is created for the remainder; the webhook settles it.

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
        if existing is not None and existing.paylink_url is not None:
            return PayResult(
                invoice_id=invoice.id,
                status="PENDING",
                amount_from_wallet=invoice.amount_from_wallet,
                amount_from_gateway=existing.amount,
                payment_url=existing.paylink_url,
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
        charge = await self._gateway.create_charge(
            amount=gateway_amount, order_number=str(intent.id), callback_url=self._callback_url()
        )
        await self._payments.attach_gateway(
            intent, transaction_no=charge.transaction_no, url=charge.payment_url
        )
        await self._invoices.flush()
        return PayResult(
            invoice_id=invoice.id,
            status="PENDING",
            amount_from_wallet=wallet_amount,
            amount_from_gateway=gateway_amount,
            payment_url=charge.payment_url,
        )

    async def handle_webhook(self, *, raw_body: bytes, signature: str) -> WebhookResult:
        """Verify and process a gateway webhook.

        The signature is verified over the RAW body (never a re-serialized dict). A Redis
        lock on the transaction number serializes concurrent duplicate deliveries.

        Raises:
            InvalidWebhookSignatureError: The HMAC does not match.
            NotFoundError: No intent matches the transaction number.
            PaymentAmountMismatchError: The webhook amount != the intent amount.
        """
        if not self._gateway.verify_webhook_signature(raw_body, signature):
            raise InvalidWebhookSignatureError()
        event = self._parse(raw_body)
        async with redis_lock(
            self._redis, f"lock:webhook:{event.transaction_no}", ttl_seconds=_WEBHOOK_LOCK_TTL
        ):
            return await self._settle_locked(event)

    async def _settle_locked(self, event: WebhookEvent) -> WebhookResult:
        intent = await self._payments.lock_intent_by_txn(event.transaction_no)
        if intent is None:
            raise NotFoundError("Unknown transaction.")
        if intent.status is not PaymentIntentStatus.NEW:
            # Already PAID/FAILED — a replay. Idempotent no-op.
            return WebhookResult(outcome="already_processed")
        if event.status.upper() != "PAID":
            await self._payments.mark_failed(intent, reason=event.status)
            return WebhookResult(outcome="failed")
        if quantize_money(event.amount) != quantize_money(intent.amount):
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
        assert intent.reference_invoice_id is not None
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

    @staticmethod
    def _parse(raw_body: bytes) -> WebhookEvent:
        try:
            data = json.loads(raw_body)
            return WebhookEvent(
                transaction_no=str(data["transaction_no"]),
                status=str(data["status"]),
                amount=parse_money(str(data["amount"])),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationDomainError("Malformed webhook payload.") from exc


def build_payment_service(
    *, session: AsyncSession, gateway: PaymentGateway, redis: Redis, settings: Settings
) -> PaymentService:
    """Assemble a PaymentService with fresh repositories bound to one session."""
    return PaymentService(
        payments=PaymentRepository(session),
        invoices=InvoiceRepository(session),
        orders=OrderRepository(session),
        wallets=WalletRepository(session),
        money=MoneyService(WalletRepository(session)),
        promos=PromoService(PromoRepository(session)),
        gateway=gateway,
        redis=redis,
        settings=settings,
    )
