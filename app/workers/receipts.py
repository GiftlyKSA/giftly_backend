"""Invoice-paid receipt sweeper (SPEC SECTION 5.3, 21).

Drains the ``idx_invoices_receipt_pending`` set (PAID invoices with no receipt yet),
sending each customer their one receipt. The sweeper is the delivery mechanism, so a
receipt survives a failed send: it stays pending until a later pass delivers it. Each
invoice is processed in its own transaction, so one failure never blocks the rest. The
scheduled task holds a Redis lock so two workers never sweep at once.
"""

from __future__ import annotations

import logging

from app.core.config import Settings, get_settings
from app.core.db import build_engine, build_session_factory
from app.core.redis import build_redis
from app.integrations.email.base import EmailClient
from app.integrations.factory import build_clients
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.user_repository import UserRepository
from app.services.receipt_service import ReceiptService
from app.workers.broker import broker

_logger = logging.getLogger("app.workers.receipts")
_LOCK_KEY = "job:send_receipts"
_LOCK_TTL_SECONDS = 300


async def send_pending_receipts(
    *,
    limit: int = 100,
    email: EmailClient | None = None,
    factory: object | None = None,
    settings: Settings | None = None,
) -> int:
    """Send every pending paid-invoice receipt; returns how many were sent.

    Collaborators are injectable for tests; by default the job builds its own engine and
    the environment's email client (a Fake outside production).
    """
    settings = settings or get_settings()
    email = email or build_clients(settings).email
    own_engine = None
    if factory is None:
        own_engine = build_engine(settings)
        factory = build_session_factory(own_engine)

    sent = 0
    try:
        async with factory() as session:  # type: ignore[operator]
            pending = await InvoiceRepository(session).list_receipt_pending(limit)
            invoice_ids = [invoice.id for invoice in pending]

        for invoice_id in invoice_ids:
            async with factory() as session:  # type: ignore[operator]
                service = ReceiptService(
                    invoices=InvoiceRepository(session),
                    orders=OrderRepository(session),
                    users=UserRepository(session),
                    email=email,
                    settings=settings,
                )
                try:
                    if await service.send_receipt(invoice_id):
                        sent += 1
                    await session.commit()
                except Exception:  # noqa: BLE001 - one bad invoice must not stall the sweep
                    await session.rollback()
                    _logger.exception("receipt send failed for invoice %s", invoice_id)
    finally:
        if own_engine is not None:
            await own_engine.dispose()

    _logger.info("receipt sweep sent %d receipt(s)", sent)
    return sent


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def deliver_pending_receipts() -> None:
    """Scheduled task: acquire a lock and drain the pending-receipt set."""
    settings = get_settings()
    redis = build_redis(settings)
    try:
        acquired = await redis.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL_SECONDS)
        if not acquired:
            _logger.info("receipt sweep already running elsewhere; skipping")
            return
        await send_pending_receipts()
    finally:
        await redis.delete(_LOCK_KEY)
        await redis.aclose()
