"""Development-only routes (SPEC SECTION 5.1, 19).

Registered ONLY when ENVIRONMENT=development (interlock layer 4). A test asserts
these return 404 in production. The simulate route fires a correctly-signed webhook
at the REAL handler; it never bypasses webhook processing.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request

from app.core.deps import get_redis, get_settings
from app.core.exceptions import NotFoundError
from app.core.money import money_str
from app.repositories.payment_repository import PaymentRepository
from app.schemas.payments import SimulatePaymentRequest, WebhookAck
from app.services.payment_service import build_payment_service

router = APIRouter(prefix="/api/dev", tags=["dev"])


@router.get("/ping", summary="Development liveness marker", status_code=200)
async def dev_ping() -> dict[str, str]:
    """Confirm dev routes are registered (development only)."""
    return {"status": "dev"}


@router.post("/streampay/simulate", response_model=WebhookAck)
async def simulate_payment(request: Request, body: SimulatePaymentRequest) -> WebhookAck:
    """Fire a correctly-signed webhook at the REAL handler (development only).

    Looks up the intent's amount, builds the exact webhook body the gateway would send,
    signs it with the fake gateway's test secret, and calls the production webhook
    handler — it never bypasses webhook processing.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        intent = await PaymentRepository(session).lock_intent_by_payment_link(body.payment_link_id)
        if intent is None:
            raise NotFoundError("Unknown StreamPay payment link.")
        amount = money_str(intent.amount)

    payload = {
        "event_type": "PAYMENT_SUCCEEDED" if body.status == "PAID" else "PAYMENT_FAILED",
        "data": {
            "payment_link": {"id": body.payment_link_id},
            "payment": {"status": body.status, "amount": amount},
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    gateway = request.app.state.clients.gateway
    signature = gateway.sign(raw_body)  # FakeStreamPayClient signs with the test secret.

    async with factory() as session:
        try:
            service = build_payment_service(
                session=session,
                gateway=gateway,
                redis=get_redis(request),
                settings=get_settings(request),
            )
            result = await service.handle_webhook(raw_body=raw_body, signature=signature)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return WebhookAck(outcome=result.outcome)
