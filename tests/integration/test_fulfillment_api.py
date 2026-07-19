"""End-to-end fulfilment: order -> pay -> deliver -> approve -> rate, plus admin dispute.

Exercises the whole escrow lifecycle over HTTP on one event loop; skips if DB/Redis are
unavailable.
"""

from __future__ import annotations

import json as _json
import os
import secrets as _secrets
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.core.jwt import create_access_token
from app.main import create_app
from app.models import CourierProfile, User
from app.models.enums import UserRole, UserStatus
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.conftest import make_test_settings

_LAT, _LNG = 21.5000, 39.2000


def _settings() -> Settings:
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if os.environ.get("REDIS_URL"):
        overrides["REDIS_URL"] = os.environ["REDIS_URL"]
    return make_test_settings(**overrides)


def _phone() -> str:
    return f"+96650{_secrets.randbelow(10_000_000):07d}"


def _future() -> str:
    return (date.today() + timedelta(days=30)).isoformat()


def _txn(url: str) -> str:
    return parse_qs(urlparse(url).query)["transaction_no"][0]


async def _settle(client: AsyncClient, app: object, txn: str, amount: str) -> None:
    body = _json.dumps({"transaction_no": txn, "status": "PAID", "amount": amount}).encode()
    sig = app.state.clients.gateway.sign(body)  # type: ignore[attr-defined]
    await client.post(
        "/api/webhooks/paylink",
        headers={"X-Paylink-Signature": sig, "Content-Type": "application/json"},
        content=body,
    )


async def _register(
    client: AsyncClient, app: object, phone: str, role: str, **extra: object
) -> dict:
    await client.post("/api/auth/send-otp", json={"phone": phone})
    otp = app.state.clients.sms.last_otp[phone]  # type: ignore[attr-defined]
    verify = await client.post("/api/auth/verify-otp", json={"phone": phone, "otp": otp})
    reg = verify.json()["registration_token"]
    resp = await client.post(
        "/api/auth/register", json={"registration_token": reg, "role": role, **extra}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(client: AsyncClient, app: object, phone: str) -> dict:
    await client.post("/api/auth/send-otp", json={"phone": phone})
    otp = app.state.clients.sms.last_otp[phone]  # type: ignore[attr-defined]
    verify = await client.post("/api/auth/verify-otp", json={"phone": phone, "otp": otp})
    return verify.json()


async def _verify_courier(factory: object, phone: str) -> None:
    async with factory() as s:  # type: ignore[operator]
        user = await s.scalar(select(User).where(User.phone == phone))
        profile = await s.get(CourierProfile, user.id)
        profile.is_verified = True
        user.status = UserStatus.ACTIVE
        await s.commit()


async def _make_stack() -> tuple[Settings, object, object]:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as s:
            await s.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")
    return settings, engine, factory


_ITEMS = [
    {"title": "Vase", "unit_price_amount": "400.00", "quantity": 1, "tax_rate": "0.15"},
    {"title": "Wrapping", "unit_price_amount": "50.00", "quantity": 2, "tax_rate": "0.15"},
]


async def _paid_in_progress(
    client: AsyncClient, app: object, factory: object
) -> tuple[dict, dict, str]:
    """Drive an order to IN_PROGRESS (paid from a topped-up wallet)."""
    cust = await _register(client, app, _phone(), "CUSTOMER")
    courier_phone = _phone()
    await _register(client, app, courier_phone, "COURIER", city="Jeddah", national_id=_phone()[1:])
    await _verify_courier(factory, courier_phone)
    courier = await _login(client, app, courier_phone)
    cust_h = {"Authorization": f"Bearer {cust['access_token']}"}
    cour_h = {"Authorization": f"Bearer {courier['access_token']}"}

    created = await client.post(
        "/api/orders",
        headers=cust_h,
        json={
            "delivery_city": "Jeddah",
            "latitude": _LAT,
            "longitude": _LNG,
            "delivery_date": _future(),
            "request_media_keys": [],
        },
    )
    order_id = created.json()["id"]
    await client.post(f"/api/orders/{order_id}/accept", headers=cour_h)
    inv = await client.post(
        f"/api/orders/{order_id}/invoices",
        headers=cour_h,
        json={"items": _ITEMS, "courier_fee_amount": "100.00"},
    )
    assert inv.status_code == 201, inv.text

    top = await client.post("/api/wallets/topup", headers=cust_h, json={"amount": "1000.00"})
    await _settle(client, app, _txn(top.json()["payment_url"]), "1000.00")
    pay = await client.post(f"/api/invoices/{inv.json()['id']}/pay", headers=cust_h)
    assert pay.json()["status"] == "PAID", pay.text
    return cust_h, cour_h, order_id


async def _proof_key(client: AsyncClient, headers: dict) -> str:
    up = await client.post(
        "/api/media/upload-urls",
        headers=headers,
        json={"purpose": "DELIVERY_PROOF", "content_type": "image/jpeg", "byte_size": 1000},
    )
    assert up.status_code == 201, up.text
    return up.json()["storage_key"]


async def test_full_lifecycle_deliver_approve_rate() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust_h, cour_h, order_id = await _paid_in_progress(client, app, factory)

            # A far-away delivery is rejected by the geofence.
            key = await _proof_key(client, cour_h)
            far = await client.post(
                f"/api/orders/{order_id}/deliver",
                headers=cour_h,
                json={"latitude": 24.7, "longitude": 46.7, "proof_media_keys": [key]},
            )
            assert far.status_code == 422

            key2 = await _proof_key(client, cour_h)
            delivered = await client.post(
                f"/api/orders/{order_id}/deliver",
                headers=cour_h,
                json={
                    "latitude": _LAT,
                    "longitude": _LNG,
                    "proof_media_keys": [key2],
                    "note": "left with the concierge",
                },
            )
            assert delivered.status_code == 200, delivered.text
            assert delivered.json()["status"] == "DELIVERED"

            approved = await client.post(f"/api/orders/{order_id}/approve", headers=cust_h)
            assert approved.status_code == 200 and approved.json()["status"] == "COMPLETED"

            # The courier's wallet received the payout.
            cour_wallet = await client.get("/api/wallets/me", headers=cour_h)
            assert cour_wallet.json()["balance"] == "540.00"

            # Both parties rate each other, once.
            r1 = await client.post(
                f"/api/orders/{order_id}/ratings",
                headers=cust_h,
                json={"score": 5, "comment": "Great courier"},
            )
            assert r1.status_code == 201
            dup = await client.post(
                f"/api/orders/{order_id}/ratings", headers=cust_h, json={"score": 1}
            )
            assert dup.status_code == 409  # already rated
            await client.post(f"/api/orders/{order_id}/ratings", headers=cour_h, json={"score": 4})

            rated_user = r1.json()["rated_user_id"]
            summary = await client.get(f"/api/users/{rated_user}/ratings/summary", headers=cust_h)
            assert summary.json()["count"] == 1 and summary.json()["average_score"] == "5.00"
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def test_dispute_and_admin_resolves_refund() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust_h, _cour_h, order_id = await _paid_in_progress(client, app, factory)

            disputed = await client.post(
                f"/api/orders/{order_id}/dispute",
                headers=cust_h,
                json={"reason": "The gift was damaged on arrival."},
            )
            assert disputed.status_code == 201, disputed.text
            dispute_id = disputed.json()["id"]

            # Mint an ADMIN token for a seeded admin user.
            async with factory() as s:
                admin = User(phone=_phone(), role=UserRole.ADMIN, status=UserStatus.ACTIVE)
                s.add(admin)
                await s.commit()
                admin_id = admin.id
            token, _jti, _ttl = create_access_token(settings, user_id=admin_id, role="ADMIN")
            admin_h = {"Authorization": f"Bearer {token}"}

            resolved = await client.post(
                f"/api/admin/disputes/{dispute_id}/resolve",
                headers=admin_h,
                json={"outcome": "RESOLVED_CUSTOMER", "note": "Refunded in full."},
            )
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["status"] == "RESOLVED_CUSTOMER"

            order = await client.get(f"/api/orders/{order_id}", headers=cust_h)
            assert order.json()["status"] == "REFUNDED"
            # The customer paid 1000 - 724.50, then was refunded 724.50 -> back to 1000.
            wallet = await client.get("/api/wallets/me", headers=cust_h)
            assert wallet.json()["balance"] == "1000.00"

            # A non-admin cannot resolve.
            forbidden = await client.post(
                f"/api/admin/disputes/{dispute_id}/resolve",
                headers=cust_h,
                json={"outcome": "RESOLVED_COURIER"},
            )
            assert forbidden.status_code == 403
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()
