"""End-to-end auth tests: OTP -> register -> tokens -> refresh rotation + reuse.

Covers the SPEC SECTION 20.A flow and the mandated negative cases: expired JWT,
alg:none JWT, logout denylist, and refresh-token reuse revoking the family. Runs on a
single event loop via httpx's ASGI transport; skips if DB/Redis are unavailable.
"""

from __future__ import annotations

import base64
import json
import os
import secrets as _secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.main import create_app
from app.models import CourierProfile, RefreshToken, User, Wallet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from tests.conftest import make_test_settings

_JWT_SECRET = "test-jwt-secret-value-not-real-000000000000"


def _settings() -> Settings:
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if os.environ.get("REDIS_URL"):
        overrides["REDIS_URL"] = os.environ["REDIS_URL"]
    return make_test_settings(**overrides)


def _new_phone() -> str:
    return f"+96650{_secrets.randbelow(10_000_000):07d}"


async def _register_customer(client: AsyncClient, app: object, phone: str) -> dict:
    await client.post("/api/auth/send-otp", json={"phone": phone})
    otp = app.state.clients.sms.last_otp[phone]  # type: ignore[attr-defined]
    verify = await client.post("/api/auth/verify-otp", json={"phone": phone, "otp": otp})
    assert verify.status_code == 200
    reg_token = verify.json()["registration_token"]
    resp = await client.post(
        "/api/auth/register",
        json={"registration_token": reg_token, "role": "CUSTOMER", "full_name": "Nora"},
    )
    assert resp.status_code == 201
    return resp.json()


async def _cleanup(factory: object, phones: list[str]) -> None:
    async with factory() as session:  # type: ignore[operator]
        for phone in phones:
            user = await session.scalar(select(User).where(User.phone == phone))
            if user is None:
                continue
            await session.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
            await session.execute(delete(CourierProfile).where(CourierProfile.user_id == user.id))
            await session.execute(delete(Wallet).where(Wallet.user_id == user.id))
            await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


async def test_auth_full_flow_and_negatives() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")

    customer_phone = _new_phone()
    courier_phone = _new_phone()
    fresh_phone = _new_phone()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            # --- Customer registration + profile fetch ---
            tokens = await _register_customer(client, app, customer_phone)
            assert tokens["role"] == "CUSTOMER"
            access, refresh = tokens["access_token"], tokens["refresh_token"]

            auth_header = {"Authorization": f"Bearer {access}"}
            me = await client.get("/api/users/me", headers=auth_header)
            assert me.status_code == 200
            assert me.json()["phone"] == customer_phone

            # --- Editing the profile takes effect immediately ---
            patched = await client.patch(
                "/api/users/me",
                headers=auth_header,
                json={"full_name": "Nora Updated", "email": "nora@example.com"},
            )
            assert patched.status_code == 200
            assert patched.json()["full_name"] == "Nora Updated"
            assert patched.json()["email"] == "nora@example.com"

            # --- No token / bad token is 401 ---
            assert (await client.get("/api/users/me")).status_code == 401

            # --- alg:none forgery is rejected ---
            forged = _alg_none_token(me.json()["id"])
            assert (
                await client.get("/api/users/me", headers={"Authorization": f"Bearer {forged}"})
            ).status_code == 401

            # --- expired token is rejected ---
            expired = _expired_token(me.json()["id"])
            assert (
                await client.get("/api/users/me", headers={"Authorization": f"Bearer {expired}"})
            ).status_code == 401

            # --- refresh rotation: old token becomes invalid, new one works ---
            r1 = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
            assert r1.status_code == 200
            new_refresh = r1.json()["refresh_token"]
            assert new_refresh != refresh

            # --- reuse of the old refresh token revokes the whole family ---
            reuse = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
            assert reuse.status_code == 401
            after = await client.post("/api/auth/refresh", json={"refresh_token": new_refresh})
            assert after.status_code == 401  # family revoked

            # --- logout denylists the access token ---
            fresh = await _register_customer(client, app, fresh_phone)
            fresh_access = fresh["access_token"]
            out = await client.post(
                "/api/auth/logout", headers={"Authorization": f"Bearer {fresh_access}"}
            )
            assert out.status_code == 204
            gone = await client.get(
                "/api/users/me", headers={"Authorization": f"Bearer {fresh_access}"}
            )
            assert gone.status_code == 401

            # --- Courier registration encrypts the national id and is PENDING ---
            await client.post("/api/auth/send-otp", json={"phone": courier_phone})
            c_otp = app.state.clients.sms.last_otp[courier_phone]  # type: ignore[attr-defined]
            c_verify = await client.post(
                "/api/auth/verify-otp", json={"phone": courier_phone, "otp": c_otp}
            )
            c_reg = c_verify.json()["registration_token"]
            c_resp = await client.post(
                "/api/auth/register",
                json={
                    "registration_token": c_reg,
                    "role": "COURIER",
                    "full_name": "Fahad",
                    "city": "Jeddah",
                    "national_id": "1122334455",
                },
            )
            assert c_resp.status_code == 201
            assert c_resp.json()["role"] == "COURIER"

        async with factory() as session:
            courier = await session.scalar(select(User).where(User.phone == courier_phone))
            profile = await session.get(CourierProfile, courier.id)
            assert profile is not None
            assert profile.national_id_encrypted and profile.national_id_encrypted != "1122334455"
            assert profile.is_verified is False
    finally:
        await _cleanup(factory, [customer_phone, courier_phone, fresh_phone])
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()


def _b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _alg_none_token(sub: str) -> str:
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload = _b64url(
        {
            "sub": sub,
            "role": "CUSTOMER",
            "jti": str(uuid.uuid4()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "iss": "safe-gift",
            "aud": "safe-gift",
        }
    )
    return f"{header}.{payload}."


def _expired_token(sub: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": sub,
            "role": "CUSTOMER",
            "jti": str(uuid.uuid4()),
            "iat": int((now - timedelta(hours=2)).timestamp()),
            "exp": int((now - timedelta(hours=1)).timestamp()),
            "iss": "safe-gift",
            "aud": "safe-gift",
        },
        _JWT_SECRET,
        algorithm="HS256",
    )
