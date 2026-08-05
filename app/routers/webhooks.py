"""StreamPay webhook routes (SPEC SECTION 5.1, 17.2 A08)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_redis, get_settings
from app.schemas.payments import WebhookAck
from app.services.payment_service import build_payment_service

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/streampay", response_model=WebhookAck)
async def streampay_webhook(
    request: Request,
    x_webhook_signature: Annotated[str, Header()] = "",
) -> WebhookAck:
    """Verify and process a StreamPay callback (signature over the raw body)."""
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
            result = await service.handle_webhook(raw_body=raw_body, signature=x_webhook_signature)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return WebhookAck(outcome=result.outcome)
