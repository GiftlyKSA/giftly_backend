"""Development-only routes (SPEC SECTION 5.1, 19).

Registered ONLY when ENVIRONMENT=development (interlock layer 4). A test asserts
these return 404 in production. The simulate route fires a correctly-signed webhook
at the REAL handler; it never bypasses webhook processing.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/dev", tags=["dev"])


@router.get("/ping", summary="Development liveness marker", status_code=200)
async def dev_ping() -> dict[str, str]:
    """Confirm dev routes are registered (development only)."""
    return {"status": "dev"}
