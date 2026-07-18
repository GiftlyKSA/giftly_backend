"""End-to-end payment tests: top-up, wallet/gateway invoice payment, webhook idempotency.

The dev simulate route fires a correctly-signed webhook at the REAL handler, so these
exercise the whole gateway path with no Paylink credentials. Runs on one event loop via
httpx's ASGI transport; skips if DB/Redis are unavailable.
"""

from __future__ import annotations

import os
import secrets as _secrets
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.main import create_app
from app.models import CourierProfile, User
from app.models.enums import UserStatus
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


def _txn_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query)["transaction_no"][0]


async def _settle(
    client: AsyncClient, app: object, txn: str, amount: str, status: str = "PAID"
) -> dict:
    """Post a correctly-signed webhook at the real handler and return its JSON."""
    import json as _json

    body = _json.dumps({"transaction_no": txn, "status": status, "amount": amount}).encode("utf-8")
    signature = app.state.clients.gateway.sign(body)  # type: ignore[attr-defined]
    resp = await client.post(
        "/api/webhooks/paylink",
        headers={"X-Paylink-Signature": signature, "Content-Type": "application/json"},
        content=body,
    )
    return {"status_code": resp.status_code, "json": resp.json() if resp.content else {}}


_GOLDEN_ITEMS = [
    {"title": "Vase", "unit_price_amount": "400.00", "quantity": 1, "tax_rate": "0.15"},
    {"title": "Wrapping", "unit_price_amount": "50.00", "quantity": 2, "tax_rate": "0.15"},
]


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


async def _issued_invoice(
    client: AsyncClient, app: object, factory: object
) -> tuple[dict, dict, str, str]:
    """Register a customer + verified courier, assign an order, and issue an invoice."""
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
            "latitude": 21.5,
            "longitude": 39.2,
            "delivery_date": _future(),
            "request_media_keys": [],
        },
    )
    order_id = created.json()["id"]
    await client.post(f"/api/orders/{order_id}/accept", headers=cour_h)
    inv = await client.post(
        f"/api/orders/{order_id}/invoices",
        headers=cour_h,
        json={"items": _GOLDEN_ITEMS, "courier_fee_amount": "100.00"},
    )
    assert inv.status_code == 201, inv.text
    assert inv.json()["total_amount"] == "724.50"
    return cust_h, cour_h, order_id, inv.json()["id"]


async def test_topup_credits_wallet_on_webhook() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust = await _register(client, app, _phone(), "CUSTOMER")
            h = {"Authorization": f"Bearer {cust['access_token']}"}

            top = await client.post("/api/wallets/topup", headers=h, json={"amount": "500.00"})
            assert top.status_code == 201, top.text
            txn = _txn_from_url(top.json()["payment_url"])

            # Before settlement the wallet is empty.
            assert (await client.get("/api/wallets/me", headers=h)).json()["balance"] == "0.00"

            sim = await _settle(client, app, txn, "500.00")
            assert sim["status_code"] == 200 and sim["json"]["outcome"] == "processed"

            wallet = await client.get("/api/wallets/me", headers=h)
            assert wallet.json()["balance"] == "500.00"
            assert wallet.json()["available"] == "500.00"

            # A duplicate webhook is idempotent — no double credit.
            again = await _settle(client, app, txn, "500.00")
            assert again["json"]["outcome"] == "already_processed"
            assert (await client.get("/api/wallets/me", headers=h)).json()["balance"] == "500.00"
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def test_pay_invoice_from_wallet_settles_immediately() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust_h, _cour_h, order_id, invoice_id = await _issued_invoice(client, app, factory)

            # Fund the wallet with more than the total (724.50).
            top = await client.post(
                "/api/wallets/topup", headers=cust_h, json={"amount": "1000.00"}
            )
            await _settle(client, app, _txn_from_url(top.json()["payment_url"]), "1000.00")

            pay = await client.post(f"/api/invoices/{invoice_id}/pay", headers=cust_h)
            assert pay.status_code == 200, pay.text
            assert pay.json()["status"] == "PAID"
            assert pay.json()["amount_from_wallet"] == "724.50"
            assert pay.json()["amount_from_gateway"] == "0.00"
            assert pay.json()["payment_url"] is None

            # The invoice is PAID and the order is IN_PROGRESS.
            inv = await client.get(f"/api/invoices/{invoice_id}", headers=cust_h)
            assert inv.json()["status"] == "PAID"
            order = await client.get(f"/api/orders/{order_id}", headers=cust_h)
            assert order.json()["status"] == "IN_PROGRESS"

            # The wallet was debited by the total.
            assert (await client.get("/api/wallets/me", headers=cust_h)).json()[
                "balance"
            ] == "275.50"
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def test_pay_invoice_via_gateway_settles_on_webhook() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust_h, _cour_h, order_id, invoice_id = await _issued_invoice(client, app, factory)

            # No wallet balance — the whole total is due from the gateway.
            pay = await client.post(f"/api/invoices/{invoice_id}/pay", headers=cust_h)
            assert pay.status_code == 200, pay.text
            assert pay.json()["status"] == "PENDING"
            assert pay.json()["amount_from_gateway"] == "724.50"
            assert pay.json()["payment_url"] is not None

            # Still WAITING_PAYMENT until the gateway confirms.
            order = await client.get(f"/api/orders/{order_id}", headers=cust_h)
            assert order.json()["status"] == "WAITING_PAYMENT"

            sim = await _settle(client, app, _txn_from_url(pay.json()["payment_url"]), "724.50")
            assert sim["json"]["outcome"] == "processed"

            inv = await client.get(f"/api/invoices/{invoice_id}", headers=cust_h)
            assert inv.json()["status"] == "PAID"
            order = await client.get(f"/api/orders/{order_id}", headers=cust_h)
            assert order.json()["status"] == "IN_PROGRESS"
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def test_webhook_rejects_bad_signature() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            resp = await client.post(
                "/api/webhooks/paylink",
                headers={"X-Paylink-Signature": "deadbeef"},
                json={"transaction_no": "FAKE-TXN-00000001", "status": "PAID", "amount": "500.00"},
            )
            assert resp.status_code == 401
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def test_dev_simulate_route_settles_topup() -> None:
    settings, engine, _factory = await _make_stack()
    # The dev simulate route is registered only in development.
    dev_settings = make_test_settings(
        ENVIRONMENT="development",
        DATABASE_URL=settings.DATABASE_URL.get_secret_value(),
        REDIS_URL=settings.REDIS_URL.get_secret_value(),
    )
    app = create_app(dev_settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust = await _register(client, app, _phone(), "CUSTOMER")
            h = {"Authorization": f"Bearer {cust['access_token']}"}
            top = await client.post("/api/wallets/topup", headers=h, json={"amount": "300.00"})
            txn = _txn_from_url(top.json()["payment_url"])

            sim = await client.post("/api/dev/paylink/simulate", json={"transaction_no": txn})
            assert sim.status_code == 200 and sim.json()["outcome"] == "processed"
            assert (await client.get("/api/wallets/me", headers=h)).json()["balance"] == "300.00"

            # An unknown transaction is a 404.
            missing = await client.post(
                "/api/dev/paylink/simulate", json={"transaction_no": "NOPE-404"}
            )
            assert missing.status_code == 404
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()
