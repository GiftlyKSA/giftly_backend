"""Health endpoints (SPEC SECTION 19).

``/api/health`` is a pure liveness probe with no dependencies so an orchestrator
never restarts the container over a transient Redis blip. ``/api/health/ready``
checks the backing services and may fail without implying the process is unhealthy.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("", summary="Liveness probe", status_code=200)
async def health() -> dict[str, str]:
    """Return a static liveness marker with no dependency checks."""
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe", status_code=200)
async def ready() -> dict[str, str]:
    """Return readiness. Dependency checks are wired in as services are added.

    Note:
        Kept dependency-free until the DB/Redis/S3 sessions land so the skeleton is
        runnable; the readiness checks are a Phase 2+ concern.
    """
    return {"status": "ready"}
