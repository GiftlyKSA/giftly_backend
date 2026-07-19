"""Health endpoints (SPEC SECTION 19).

``/api/health`` is a pure liveness probe with no dependencies so an orchestrator
never restarts the container over a transient Redis blip. ``/api/health/ready``
checks the backing services (database and Redis) and returns 503 when one is down,
which may fail without implying the process itself is unhealthy.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(prefix="/api/health", tags=["health"])

_logger = logging.getLogger("app.health")


@router.get("", summary="Liveness probe", status_code=200)
async def health() -> dict[str, str]:
    """Return a static liveness marker with no dependency checks."""
    return {"status": "ok"}


async def _check_database(request: Request) -> bool:
    """Return True if a trivial query round-trips through the database."""
    try:
        factory = request.app.state.session_factory
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — readiness reports the failure, never raises.
        _logger.warning("Readiness: database check failed", exc_info=True)
        return False


async def _check_redis(request: Request) -> bool:
    """Return True if Redis answers a PING."""
    try:
        return bool(await request.app.state.redis.ping())
    except Exception:  # noqa: BLE001 — readiness reports the failure, never raises.
        _logger.warning("Readiness: redis check failed", exc_info=True)
        return False


@router.get("/ready", summary="Readiness probe")
async def ready(request: Request) -> JSONResponse:
    """Probe the backing services and report readiness.

    Returns:
        200 with each dependency marked ``ok`` when all pass; 503 with the failing
        dependency marked ``down`` otherwise. The probe never raises, so a dependency
        outage is a clean 503 rather than a 500.
    """
    checks = {
        "database": "ok" if await _check_database(request) else "down",
        "redis": "ok" if await _check_redis(request) else "down",
    }
    is_ready = all(state == "ok" for state in checks.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ready" if is_ready else "unavailable", "checks": checks},
    )
