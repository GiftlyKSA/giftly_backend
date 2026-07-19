"""Expiry sweeper for unpaid invoices and stale payment intents (SPEC SECTION 13, 21).

Two kinds of stale gateway state are cleaned up:

* An ISSUED invoice whose payment window lapsed is EXPIRED — its held wallet funds are
  released, its open gateway intent is EXPIRED, its promo reservation is returned, and the
  order reopens to ASSIGNED so the courier can re-issue.
* A NEW wallet-top-up intent past its expiry is simply EXPIRED (no money was held).

Each item settles in its own transaction (one failure never blocks the rest); the
scheduled task holds a Redis lock so two workers never sweep at once. Money moves only
through the ledger; releasing a hold is a reservation change, not a ledger movement.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.core.db import build_engine, build_session_factory
from app.core.money import ZERO
from app.core.redis import build_redis
from app.models.enums import (
    InvoiceStatus,
    OrderStatus,
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
from app.workers.broker import broker

_logger = logging.getLogger("app.workers.expiry")
_LOCK_KEY = "job:expire_stale"
_LOCK_TTL_SECONDS = 300


async def expire_stale(
    *, limit: int = 200, factory: object | None = None, settings: Settings | None = None
) -> tuple[int, int]:
    """Expire lapsed invoices and top-up intents; returns (invoices, intents) expired."""
    settings = settings or get_settings()
    own_engine = None
    if factory is None:
        own_engine = build_engine(settings)
        factory = build_session_factory(own_engine)

    now = datetime.now(UTC)
    invoices_expired = 0
    intents_expired = 0
    try:
        async with factory() as session:  # type: ignore[operator]
            invoice_ids = [
                inv.id
                for inv in await InvoiceRepository(session).list_expired_issued(
                    now=now, limit=limit
                )
            ]
            topup_ids = [
                i.id
                for i in await PaymentRepository(session).list_expired_new(now=now, limit=limit)
                if i.purpose is PaymentPurpose.WALLET_TOPUP
            ]

        for invoice_id in invoice_ids:
            async with factory() as session:  # type: ignore[operator]
                if await _expire_invoice(session, invoice_id):
                    invoices_expired += 1
        for intent_id in topup_ids:
            async with factory() as session:  # type: ignore[operator]
                if await _expire_topup_intent(session, intent_id):
                    intents_expired += 1
    finally:
        if own_engine is not None:
            await own_engine.dispose()

    _logger.info(
        "expiry sweep: %d invoice(s), %d top-up intent(s)", invoices_expired, intents_expired
    )
    return invoices_expired, intents_expired


async def _expire_invoice(session: object, invoice_id: uuid.UUID) -> bool:
    invoices = InvoiceRepository(session)  # type: ignore[arg-type]
    invoice = await invoices.lock(invoice_id)
    if invoice is None or invoice.status is not InvoiceStatus.ISSUED:
        return False
    try:
        payments = PaymentRepository(session)  # type: ignore[arg-type]
        wallets = WalletRepository(session)  # type: ignore[arg-type]
        # Release the held wallet portion and expire the open gateway intent, if any.
        intent = await payments.get_open_intent_for_invoice(invoice.id)
        if intent is not None:
            if invoice.amount_from_wallet > ZERO:
                wallet = await wallets.get_by_user(intent.user_id)
                if wallet is not None:
                    await MoneyService(wallets).release_hold(
                        wallet_id=wallet.id, amount=invoice.amount_from_wallet
                    )
            await payments.mark_expired(intent)

        await PromoService(PromoRepository(session)).release(invoice_id=invoice.id)  # type: ignore[arg-type]
        invoice.status = InvoiceStatus.EXPIRED

        order = await OrderRepository(session).lock(invoice.order_id)  # type: ignore[arg-type]
        if order is not None and order.status is OrderStatus.WAITING_PAYMENT:
            assert_transition(order.status, OrderStatus.ASSIGNED)
            order.status = OrderStatus.ASSIGNED
            order.total_amount = ZERO
        await invoices.flush()
        await session.commit()  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001 - one bad invoice must not stall the sweep
        await session.rollback()  # type: ignore[attr-defined]
        _logger.exception("failed to expire invoice %s", invoice_id)
        return False


async def _expire_topup_intent(session: object, intent_id: uuid.UUID) -> bool:
    payments = PaymentRepository(session)  # type: ignore[arg-type]
    intent = await payments.lock_intent(intent_id)
    if intent is None or intent.purpose is not PaymentPurpose.WALLET_TOPUP:
        return False
    try:
        await payments.mark_expired(intent)
        await session.commit()  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001
        await session.rollback()  # type: ignore[attr-defined]
        _logger.exception("failed to expire intent %s", intent_id)
        return False


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
async def run_expire_stale() -> None:
    """Scheduled task: acquire a lock and expire lapsed invoices/intents."""
    settings = get_settings()
    redis = build_redis(settings)
    try:
        acquired = await redis.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL_SECONDS)
        if not acquired:
            _logger.info("expiry sweep already running elsewhere; skipping")
            return
        await expire_stale()
    finally:
        await redis.delete(_LOCK_KEY)
        await redis.aclose()
