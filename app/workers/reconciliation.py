"""Nightly ledger reconciliation job (SPEC SECTION 21).

Asserts, for every wallet, ``balance == SUM(settled amounts)`` and, for every
correlation group, that the settled amounts sum to 0.00. Any drift is logged at ERROR
so monitoring pages on it — this is the last line of defence for money. The job is
idempotent (a pure read) and holds a Redis lock so two workers never run it at once.
"""

from __future__ import annotations

import logging

from app.core.config import get_settings
from app.core.db import build_engine, build_session_factory
from app.core.locks import LockNotAcquiredError, redis_lock
from app.core.redis import build_redis
from app.repositories.wallet_repository import WalletRepository
from app.services.money_service import MoneyService, ReconcileReport
from app.workers.broker import broker

_logger = logging.getLogger("app.workers.reconciliation")
_LOCK_KEY = "job:reconcile_ledger"
_LOCK_TTL_SECONDS = 600


async def run_reconciliation() -> ReconcileReport:
    """Run one reconciliation pass, logging drift; safe to call directly in tests."""
    settings = get_settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            report = await MoneyService(WalletRepository(session)).reconcile()
    finally:
        await engine.dispose()

    if report.ok:
        _logger.info(
            "reconciliation ok: %d wallets, %d correlation groups",
            report.wallets_checked,
            report.correlations_checked,
        )
    else:
        for drift in report.drifts:
            _logger.error("reconciliation drift: %s", drift)
    return report


@broker.task(schedule=[{"cron": "0 3 * * *"}])
async def reconcile_ledger() -> None:
    """Nightly task: acquire a lock, reconcile, and page on drift."""
    settings = get_settings()
    redis = build_redis(settings)
    try:
        # Lua compare-and-delete release (audit SEC-5): if this run outlives the TTL
        # and a peer re-acquires, releasing must not free the peer's lock.
        async with redis_lock(redis, _LOCK_KEY, ttl_seconds=_LOCK_TTL_SECONDS):
            await run_reconciliation()
    except LockNotAcquiredError:
        _logger.info("reconciliation already running elsewhere; skipping")
    finally:
        await redis.aclose()
