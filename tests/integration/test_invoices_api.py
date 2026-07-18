"""End-to-end invoice tests: authoring, the golden 655.50 example, promo preview, cancel.

The courier authors the invoice; the platform prices it. The golden worked example
(SPEC SECTION 11) is the regression anchor: WELCOME10 (10%, capped 100) on a 600.00
discountable base yields a 655.50 total. Runs on one event loop via httpx's ASGI
transport; skips if DB/Redis are unavailable.
"""

from __future__ import annotations

import os
import secrets as _secrets
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.main import create_app
from app.models import CourierProfile, Promo, User
from app.models.enums import PromoDiscountType, UserStatus
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.conftest import make_test_settings


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


async def _seed_promo(factory: object) -> str:
    """Seed a unique 10%-capped-100 promo and return its code."""
    code = f"WEL{uuid.uuid4().hex[:8].upper()}"
    async with factory() as s:  # type: ignore[operator]
        s.add(
            Promo(
                code=code,
                description="ten percent",
                discount_type=PromoDiscountType.PERCENT,
                percent_value=Decimal("10.00"),
                max_discount_amount=Decimal("100.00"),
                min_order_amount=Decimal("0.00"),
                max_usages_per_user=5,
            )
        )
        await s.commit()
    return code


async def _make_stack() -> tuple[object, object, object]:
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


_GOLDEN_ITEMS = [
    {
        "title": "Hand-painted ceramic vase",
        "unit_price_amount": "400.00",
        "quantity": 1,
        "tax_rate": "0.15",
    },
    {
        "title": "Gift wrapping, silk",
        "unit_price_amount": "50.00",
        "quantity": 2,
        "tax_rate": "0.15",
    },
]


async def _assigned_order(
    client: AsyncClient, app: object, factory: object
) -> tuple[dict, dict, str]:
    """Register a customer + verified courier, create an order, and accept it."""
    customer_phone, courier_phone = _phone(), _phone()
    cust = await _register(client, app, customer_phone, "CUSTOMER")
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
            "latitude": 21.5,
            "longitude": 39.2,
            "delivery_date": _future(),
            "request_media_keys": [],
        },
    )
    order_id = created.json()["id"]
    accepted = await client.post(f"/api/orders/{order_id}/accept", headers=cour_h)
    assert accepted.status_code == 200, accepted.text
    return cust_h, cour_h, order_id


async def test_invoice_golden_example_and_reads() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    code = await _seed_promo(factory)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust_h, cour_h, order_id = await _assigned_order(client, app, factory)

            resp = await client.post(
                f"/api/orders/{order_id}/invoices",
                headers=cour_h,
                json={
                    "items": _GOLDEN_ITEMS,
                    "courier_fee_amount": "100.00",
                    "promo_code": code.lower(),  # case-insensitive
                },
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            # The golden anchor.
            assert body["total_amount"] == "655.50"
            assert body["items_net_amount"] == "500.00"
            assert body["service_fee_amount"] == "30.00"
            assert body["discount_amount"] == "60.00"
            assert body["tax_amount"] == "85.50"
            assert body["net_after_discount_amount"] == "570.00"
            assert body["promo_code"] == code
            assert len(body["items"]) == 2
            invoice_id = body["id"]

            # The order moved to WAITING_PAYMENT.
            order = await client.get(f"/api/orders/{order_id}", headers=cust_h)
            assert order.json()["status"] == "WAITING_PAYMENT"
            assert order.json()["total_amount"] == "655.50"

            # The customer can read the invoice by id and via the order.
            by_id = await client.get(f"/api/invoices/{invoice_id}", headers=cust_h)
            assert by_id.status_code == 200
            assert by_id.json()["total_amount"] == "655.50"
            by_order = await client.get(f"/api/orders/{order_id}/invoice", headers=cour_h)
            assert by_order.status_code == 200 and by_order.json()["id"] == invoice_id

            # A stranger cannot read it.
            other = await _register(client, app, _phone(), "CUSTOMER")
            other_h = {"Authorization": f"Bearer {other['access_token']}"}
            leak = await client.get(f"/api/invoices/{invoice_id}", headers=other_h)
            assert leak.status_code == 404

            # A second invoice for the same order conflicts.
            dup = await client.post(
                f"/api/orders/{order_id}/invoices",
                headers=cour_h,
                json={"items": _GOLDEN_ITEMS, "courier_fee_amount": "100.00"},
            )
            assert dup.status_code == 409
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def test_promo_preview_matches_golden() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    code = await _seed_promo(factory)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust_h, cour_h, order_id = await _assigned_order(client, app, factory)

            # Issue the invoice WITHOUT a promo: total is the undiscounted 724.50.
            issued = await client.post(
                f"/api/orders/{order_id}/invoices",
                headers=cour_h,
                json={"items": _GOLDEN_ITEMS, "courier_fee_amount": "100.00"},
            )
            assert issued.status_code == 201, issued.text
            assert issued.json()["total_amount"] == "724.50"
            assert issued.json()["discount_amount"] == "0.00"
            assert issued.json()["promo_code"] is None

            # The customer previews the promo and sees the golden discounted total.
            preview = await client.post(
                "/api/promos/validate",
                headers=cust_h,
                json={"code": code, "order_id": order_id},
            )
            assert preview.status_code == 200, preview.text
            assert preview.json()["discount_amount"] == "60.00"
            assert preview.json()["original_total_amount"] == "724.50"
            assert preview.json()["total_amount"] == "655.50"
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def test_cancel_invoice_reopens_order() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust_h, cour_h, order_id = await _assigned_order(client, app, factory)
            issued = await client.post(
                f"/api/orders/{order_id}/invoices",
                headers=cour_h,
                json={"items": _GOLDEN_ITEMS, "courier_fee_amount": "100.00"},
            )
            invoice_id = issued.json()["id"]

            cancelled = await client.post(f"/api/invoices/{invoice_id}/cancel", headers=cour_h)
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "CANCELLED"

            # The order is authorable again, and a fresh invoice can be issued.
            order = await client.get(f"/api/orders/{order_id}", headers=cust_h)
            assert order.json()["status"] == "ASSIGNED"
            reissue = await client.post(
                f"/api/orders/{order_id}/invoices",
                headers=cour_h,
                json={"items": _GOLDEN_ITEMS, "courier_fee_amount": "100.00"},
            )
            assert reissue.status_code == 201
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def test_invoice_requires_assigned_courier() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            _cust_h, _cour_h, order_id = await _assigned_order(client, app, factory)
            # A different verified courier cannot invoice this order (404, no leak).
            other_phone = _phone()
            await _register(
                client, app, other_phone, "COURIER", city="Jeddah", national_id=_phone()[1:]
            )
            await _verify_courier(factory, other_phone)
            other = await _login(client, app, other_phone)
            other_h = {"Authorization": f"Bearer {other['access_token']}"}
            resp = await client.post(
                f"/api/orders/{order_id}/invoices",
                headers=other_h,
                json={"items": _GOLDEN_ITEMS, "courier_fee_amount": "100.00"},
            )
            assert resp.status_code == 404
            # An unknown order is also 404.
            missing = await client.post(
                f"/api/orders/{uuid.uuid4()}/invoices",
                headers=other_h,
                json={"items": _GOLDEN_ITEMS, "courier_fee_amount": "100.00"},
            )
            assert missing.status_code == 404
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()
