"""Auto-approve sweeper (SPEC SECTION 20.G, 21).

A delivered order the customer never acts on auto-approves after AUTO_APPROVE_HOURS,
releasing escrow to the courier so funds are never stranded. Backed by
``idx_orders_status_delivered_at``. Each order settles in its own transaction (one
failure never blocks the rest) and release is idempotent, so a customer approval racing
this job pays out exactly once. The scheduled task holds a Redis lock.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import build_engine, build_session_factory
from app.core.redis import build_redis
from app.integrations.factory import build_clients
from app.repositories.dispute_repository import DisputeRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.wallet_repository import WalletRepository
from app.services.fulfillment_service import FulfillmentService
from app.services.media_service import MediaService
from app.services.money_service import MoneyService
from app.workers.broker import broker

_logger = logging.getLogger("app.workers.auto_approve")
_LOCK_KEY = "job:auto_approve"
_LOCK_TTL_SECONDS = 300


def _service(session: AsyncSession, settings: Settings) -> FulfillmentService:
    return FulfillmentService(
        orders=OrderRepository(session),
        invoices=InvoiceRepository(session),
        disputes=DisputeRepository(session),
        wallets=WalletRepository(session),
        money=MoneyService(WalletRepository(session)),
        media=MediaService(build_clients(settings).storage, settings),
        settings=settings,
    )


async def auto_approve_delivered(
    *, limit: int = 100, factory: object | None = None, settings: Settings | None = None
) -> int:
    """Auto-approve delivered orders past the window; returns how many completed."""
    settings = settings or get_settings()
    own_engine = None
    if factory is None:
        own_engine = build_engine(settings)
        factory = build_session_factory(own_engine)

    completed = 0
    try:
        async with factory() as session:  # type: ignore[operator]
            cutoff = _service(session, settings).auto_approve_cutoff()
            due = await OrderRepository(session).list_auto_approve_due(cutoff, limit)
            order_ids = [order.id for order in due]

        for order_id in order_ids:
            async with factory() as session:  # type: ignore[operator]
                try:
                    if await _service(session, settings).auto_approve(order_id=order_id):
                        completed += 1
                    await session.commit()
                except Exception:  # noqa: BLE001 - one bad order must not stall the sweep
                    await session.rollback()
                    _logger.exception("auto-approve failed for order %s", order_id)
    finally:
        if own_engine is not None:
            await own_engine.dispose()

    _logger.info("auto-approve completed %d order(s)", completed)
    return completed


@broker.task(schedule=[{"cron": "*/15 * * * *"}])
async def run_auto_approve() -> None:
    """Scheduled task: acquire a lock and auto-approve overdue deliveries."""
    settings = get_settings()
    redis = build_redis(settings)
    try:
        acquired = await redis.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL_SECONDS)
        if not acquired:
            _logger.info("auto-approve already running elsewhere; skipping")
            return
        await auto_approve_delivered()
    finally:
        await redis.delete(_LOCK_KEY)
        await redis.aclose()
