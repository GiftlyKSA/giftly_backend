"""End-to-end chat: REST messaging over an assigned order's conversation, plus live WS.

Assigning an order opens its conversation (Phase 6). These tests send/read messages over
REST, list the inbox, and verify a message published on send reaches a connected WebSocket.
The WebSocket path uses Starlette's sync TestClient (httpx's ASGI transport has no WS).
"""

from __future__ import annotations

import os
import secrets as _secrets
from datetime import date, timedelta

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.main import create_app
from app.models import CourierProfile, User
from app.models.enums import UserStatus
from fastapi.testclient import TestClient
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


async def _assigned_conversation(
    client: AsyncClient, app: object, factory: object
) -> tuple[dict, dict, str, str, str]:
    """Register a customer + verified courier, assign an order, return its conversation id."""
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

    inbox = await client.get("/api/conversations", headers=cour_h)
    assert inbox.status_code == 200, inbox.text
    conversation_id = inbox.json()["items"][0]["conversation_id"]
    return cust_h, cour_h, order_id, conversation_id, courier["access_token"]


async def test_rest_chat_send_list_read_inbox() -> None:
    settings, engine, factory = await _make_stack()
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            cust_h, cour_h, _order_id, conv_id, _tok = await _assigned_conversation(
                client, app, factory
            )

            sent = await client.post(
                f"/api/conversations/{conv_id}/messages",
                headers=cust_h,
                json={"text": "Hi, when will you arrive?"},
            )
            assert sent.status_code == 201, sent.text
            assert sent.json()["content"] == "Hi, when will you arrive?"

            # The courier reads the thread, decrypted.
            msgs = await client.get(f"/api/conversations/{conv_id}/messages", headers=cour_h)
            assert msgs.status_code == 200
            texts = [m["content"] for m in msgs.json()["items"]]
            assert "Hi, when will you arrive?" in texts

            # The courier's inbox shows the preview and an unread count.
            inbox = await client.get("/api/conversations", headers=cour_h)
            row = next(i for i in inbox.json()["items"] if i["conversation_id"] == conv_id)
            assert row["last_message_preview"] == "Hi, when will you arrive?"
            assert row["unread_count"] >= 1

            # Marking read clears the courier's unread count.
            read = await client.post(f"/api/conversations/{conv_id}/read", headers=cour_h)
            assert read.status_code == 204
            inbox2 = await client.get("/api/conversations", headers=cour_h)
            row2 = next(i for i in inbox2.json()["items"] if i["conversation_id"] == conv_id)
            assert row2["unread_count"] == 0

            # A stranger cannot post to the conversation.
            other = await _register(client, app, _phone(), "CUSTOMER")
            other_h = {"Authorization": f"Bearer {other['access_token']}"}
            leak = await client.post(
                f"/api/conversations/{conv_id}/messages",
                headers=other_h,
                json={"text": "let me in"},
            )
            assert leak.status_code == 404
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


def test_websocket_receives_live_message() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    # Confirm the DB is reachable before spinning up the sync TestClient.
    import asyncio

    async def _probe() -> None:
        try:
            async with factory() as s:
                await s.execute(select(User.id).limit(1))
        finally:
            await engine.dispose()

    try:
        asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")

    app = create_app(settings)
    with TestClient(app) as client:
        # Build an assigned conversation over the same (sync) client.
        cust = _register_sync(client, app, _phone(), "CUSTOMER")
        courier_phone = _phone()
        _register_sync(
            client, app, courier_phone, "COURIER", city="Jeddah", national_id=_phone()[1:]
        )
        _verify_courier_sync(client, app, courier_phone)
        courier = _login_sync(client, app, courier_phone)
        cust_h = {"Authorization": f"Bearer {cust['access_token']}"}
        cour_h = {"Authorization": f"Bearer {courier['access_token']}"}

        created = client.post(
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
        client.post(f"/api/orders/{order_id}/accept", headers=cour_h)
        conv_id = client.get("/api/conversations", headers=cour_h).json()["items"][0][
            "conversation_id"
        ]

        token = courier["access_token"]
        # A bad token is rejected before the socket opens.
        with (
            pytest.raises(Exception),
            client.websocket_connect(  # noqa: PT011
                f"/api/ws/conversations/{conv_id}?token=not-a-token"
            ),
        ):
            pass

        # A member connects and receives whatever is published to the conversation channel.
        import json as _json

        import redis as _redis

        with client.websocket_connect(f"/api/ws/conversations/{conv_id}?token={token}") as ws:
            sync_redis = _redis.from_url(settings.REDIS_URL.get_secret_value())
            sync_redis.publish(
                f"chat:conversation:{conv_id}",
                _json.dumps({"content": "ping over websocket", "sender_id": "x"}),
            )
            event = ws.receive_json()
            assert event["content"] == "ping over websocket"
            sync_redis.close()


def _register_sync(client: TestClient, app: object, phone: str, role: str, **extra: object) -> dict:
    client.post("/api/auth/send-otp", json={"phone": phone})
    otp = app.state.clients.sms.last_otp[phone]  # type: ignore[attr-defined]
    verify = client.post("/api/auth/verify-otp", json={"phone": phone, "otp": otp})
    reg = verify.json()["registration_token"]
    resp = client.post(
        "/api/auth/register", json={"registration_token": reg, "role": role, **extra}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login_sync(client: TestClient, app: object, phone: str) -> dict:
    client.post("/api/auth/send-otp", json={"phone": phone})
    otp = app.state.clients.sms.last_otp[phone]  # type: ignore[attr-defined]
    return client.post("/api/auth/verify-otp", json={"phone": phone, "otp": otp}).json()


def _verify_courier_sync(client: TestClient, app: object, phone: str) -> None:
    # Use a throwaway engine (its own loop) so we never touch the app's portal-bound pool.
    import asyncio

    async def _do() -> None:
        engine = build_engine(_settings())
        factory = build_session_factory(engine)
        try:
            async with factory() as s:
                user = await s.scalar(select(User).where(User.phone == phone))
                profile = await s.get(CourierProfile, user.id)
                profile.is_verified = True
                user.status = UserStatus.ACTIVE
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(_do())
