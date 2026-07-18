"""End-to-end order tests: create, radar, accept race, coordinate privacy, cancel.

Includes the mandated accept-race concurrency test (SPEC SECTION 24): many parallel
accepts on one order yield exactly one success and the rest 409. Runs on one event
loop via httpx's ASGI transport; skips if DB/Redis are unavailable.
"""

from __future__ import annotations

import asyncio
import os
import secrets as _secrets
from datetime import date, timedelta

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


async def test_order_flow_and_coordinate_privacy() -> None:
    settings, engine, factory = await _make_stack()
    customer_phone, courier_phone = _phone(), _phone()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust = await _register(client, app, customer_phone, "CUSTOMER")
            await _register(
                client, app, courier_phone, "COURIER", city="Jeddah", national_id=_phone()[1:]
            )
            await _verify_courier(factory, courier_phone)

            cust_h = {"Authorization": f"Bearer {cust['access_token']}"}
            created = await client.post(
                "/api/orders",
                headers=cust_h,
                json={
                    "description": "Hand-painted ceramic vase, blue",
                    "delivery_city": "Jeddah",
                    "latitude": 21.5433,
                    "longitude": 39.1728,
                    "delivery_date": _future(),
                    "request_media_keys": [],
                },
            )
            assert created.status_code == 201, created.text
            order_id = created.json()["id"]
            assert created.json()["status"] == "NEW"

            # Re-login the courier to get a token reflecting ACTIVE status.
            courier = await _login(client, app, courier_phone)
            cour_h = {"Authorization": f"Bearer {courier['access_token']}"}

            # Radar shows the order but the summary carries NO coordinates.
            radar = await client.get("/api/orders/available", headers=cour_h)
            radar_row = next(o for o in radar.json()["items"] if o["id"] == order_id)
            assert "latitude" not in radar_row and "longitude" not in radar_row

            # A courier has no relationship to a NEW order yet, so the detail is 404
            # (do not confirm existence to a non-participant).
            detail_before = await client.get(f"/api/orders/{order_id}", headers=cour_h)
            assert detail_before.status_code == 404

            accepted = await client.post(f"/api/orders/{order_id}/accept", headers=cour_h)
            assert accepted.status_code == 200
            assert accepted.json()["status"] == "ASSIGNED"
            assert accepted.json()["latitude"] == pytest.approx(21.5433, abs=1e-4)

            # A second accept now conflicts (already assigned).
            again = await client.post(f"/api/orders/{order_id}/accept", headers=cour_h)
            assert again.status_code == 409
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def test_customer_can_cancel_new_order() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust = await _register(client, app, _phone(), "CUSTOMER")
            cust_h = {"Authorization": f"Bearer {cust['access_token']}"}
            created = await client.post(
                "/api/orders",
                headers=cust_h,
                json={
                    "delivery_city": "Riyadh",
                    "latitude": 24.7136,
                    "longitude": 46.6753,
                    "delivery_date": _future(),
                    "request_media_keys": [],
                },
            )
            order_id = created.json()["id"]
            cancelled = await client.post(
                f"/api/orders/{order_id}/cancel", headers=cust_h, json={"reason": "changed mind"}
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "CANCELLED"
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def test_parallel_accepts_assign_exactly_once() -> None:
    settings, engine, factory = await _make_stack()
    customer_phone, courier_phone = _phone(), _phone()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust = await _register(client, app, customer_phone, "CUSTOMER")
            await _register(
                client, app, courier_phone, "COURIER", city="Jeddah", national_id=_phone()[1:]
            )
            await _verify_courier(factory, courier_phone)
            courier = await _login(client, app, courier_phone)

            cust_h = {"Authorization": f"Bearer {cust['access_token']}"}
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
            cour_h = {"Authorization": f"Bearer {courier['access_token']}"}

            async def accept() -> int:
                r = await client.post(f"/api/orders/{order_id}/accept", headers=cour_h)
                return r.status_code

            results = await asyncio.gather(*[accept() for _ in range(50)])
            assert sum(1 for code in results if code == 200) == 1
            assert sum(1 for code in results if code == 409) == 49
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


async def _login(client: AsyncClient, app: object, phone: str) -> dict:
    await client.post("/api/auth/send-otp", json={"phone": phone})
    otp = app.state.clients.sms.last_otp[phone]  # type: ignore[attr-defined]
    verify = await client.post("/api/auth/verify-otp", json={"phone": phone, "otp": otp})
    return verify.json()
