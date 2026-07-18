"""HTTP tests for the media endpoints and order creation with request photos.

Uses the FakeStorageClient: requesting an upload URL registers the key, so a later
confirm (and order creation) sees it as uploaded — mirroring a completed client PUT.
"""

from __future__ import annotations

import os
import secrets as _secrets
from datetime import date, timedelta

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


async def _register_customer(client: AsyncClient, app: object, phone: str) -> dict:
    await client.post("/api/auth/send-otp", json={"phone": phone})
    otp = app.state.clients.sms.last_otp[phone]  # type: ignore[attr-defined]
    verify = await client.post("/api/auth/verify-otp", json={"phone": phone, "otp": otp})
    reg = verify.json()["registration_token"]
    resp = await client.post(
        "/api/auth/register", json={"registration_token": reg, "role": "CUSTOMER"}
    )
    return resp.json()


async def test_media_upload_confirm_and_order_with_photo() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as s:
            await s.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")

    phone = f"+96650{_secrets.randbelow(10_000_000):07d}"
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust = await _register_customer(client, app, phone)
            headers = {"Authorization": f"Bearer {cust['access_token']}"}

            # Request an upload URL (server generates the key).
            up = await client.post(
                "/api/media/upload-urls",
                headers=headers,
                json={
                    "purpose": "ORDER_REQUEST",
                    "content_type": "image/jpeg",
                    "byte_size": 1843200,
                },
            )
            assert up.status_code == 201
            key = up.json()["storage_key"]
            assert key.startswith("orders/pending/")

            # Confirm succeeds (the fake registered the object on URL creation).
            conf = await client.post(
                "/api/media/confirm", headers=headers, json={"storage_key": key}
            )
            assert conf.status_code == 200 and conf.json()["confirmed"] is True

            # An oversized request is rejected.
            too_big = await client.post(
                "/api/media/upload-urls",
                headers=headers,
                json={
                    "purpose": "ORDER_REQUEST",
                    "content_type": "image/jpeg",
                    "byte_size": 99_999_999,
                },
            )
            assert too_big.status_code == 400

            # A path-traversal key is rejected on confirm.
            bad = await client.post(
                "/api/media/confirm",
                headers=headers,
                json={"storage_key": "orders/pending/../../etc/passwd"},
            )
            assert bad.status_code == 400

            # The confirmed photo can be attached to a new order.
            created = await client.post(
                "/api/orders",
                headers=headers,
                json={
                    "delivery_city": "Jeddah",
                    "latitude": 21.5,
                    "longitude": 39.2,
                    "delivery_date": (date.today() + timedelta(days=15)).isoformat(),
                    "request_media_keys": [key],
                },
            )
            assert created.status_code == 201, created.text
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()
