"""HTTP tests for device-token registration."""

from __future__ import annotations

import os
import secrets as _secrets

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.main import create_app
from app.models import User
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


async def _register(client: AsyncClient, app: object, phone: str) -> dict:
    await client.post("/api/auth/send-otp", json={"phone": phone})
    otp = app.state.clients.sms.last_otp[phone]  # type: ignore[attr-defined]
    verify = await client.post("/api/auth/verify-otp", json={"phone": phone, "otp": otp})
    reg = verify.json()["registration_token"]
    resp = await client.post(
        "/api/auth/register", json={"registration_token": reg, "role": "CUSTOMER"}
    )
    return resp.json()


async def test_register_and_unregister_device() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as s:
            await s.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")

    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust = await _register(client, app, _phone())
            h = {"Authorization": f"Bearer {cust['access_token']}"}
            token = f"fcm-{_secrets.token_hex(8)}"

            reg = await client.post(
                "/api/devices", headers=h, json={"token": token, "device_os": "IOS"}
            )
            assert reg.status_code == 201, reg.text
            assert reg.json()["token"] == token

            bad = await client.post(
                "/api/devices", headers=h, json={"token": token, "device_os": "SYMBIAN"}
            )
            assert bad.status_code == 422

            unreg = await client.request("DELETE", "/api/devices", headers=h, json={"token": token})
            assert unreg.status_code == 204
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()
