"""Gateway webhook routes (SPEC SECTION 5.1, 17.2 A08).

The Paylink webhook is PUBLIC (no JWT) and authenticated only by its HMAC signature,
verified over the RAW request body. Never re-serialize the payload before verifying —
that changes bytes and breaks the signature.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_redis, get_settings
from app.core.exceptions import ForbiddenError
from app.schemas.payments import WebhookAck
from app.services.payment_service import build_payment_service

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def _enforce_ip_allowlist(request: Request) -> None:
    """Reject callers outside PAYLINK_ALLOWED_IPS when the allowlist is set.

    Defence in depth over the HMAC (audit SEC-2): the setting is required in
    production, so the promise the config makes is now actually kept. An empty or
    unset value (development/test) disables the check.

    Raises:
        ForbiddenError: The source address is not on the allowlist.
    """
    raw = get_settings(request).PAYLINK_ALLOWED_IPS
    allowed = {ip.strip() for ip in (raw or "").split(",") if ip.strip()}
    if not allowed:
        return
    client_ip = request.client.host if request.client else None
    if client_ip not in allowed:
        raise ForbiddenError("Webhook source address is not allowed.")


@router.post("/paylink", response_model=WebhookAck)
async def paylink_webhook(
    request: Request,
    x_paylink_signature: Annotated[str, Header()] = "",
) -> WebhookAck:
    """Verify and process a Paylink payment callback (signature over the raw body)."""
    _enforce_ip_allowlist(request)
    raw_body = await request.body()
    # The webhook manages its own transaction; use a dedicated session that commits.
    factory = request.app.state.session_factory
    session: AsyncSession
    async with factory() as session:
        try:
            service = build_payment_service(
                session=session,
                gateway=request.app.state.clients.gateway,
                redis=get_redis(request),
                settings=get_settings(request),
            )
            result = await service.handle_webhook(raw_body=raw_body, signature=x_paylink_signature)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return WebhookAck(outcome=result.outcome)
