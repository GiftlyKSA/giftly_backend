"""Tests for the audit-fix batch: ban revocation, webhook IPs, guards, and purge."""

from __future__ import annotations

import os
import secrets as _secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.models import RefreshToken, User
from app.models.enums import UserRole
from app.repositories.auth_repository import AuthRepository
from app.services.otp_service import OtpService
from app.workers.expiry import purge_refresh_tokens
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.conftest import make_test_settings


def _settings(**overrides: object) -> Settings:
    if os.environ.get("DATABASE_URL"):
        overrides.setdefault("DATABASE_URL", os.environ["DATABASE_URL"])
    if os.environ.get("REDIS_URL"):
        overrides.setdefault("REDIS_URL", os.environ["REDIS_URL"])
    return make_test_settings(**overrides)


async def _skip_unless_db(settings: Settings) -> None:
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as s:
            await s.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    finally:
        await engine.dispose()


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


async def test_ban_revokes_live_access_and_refresh() -> None:
    """SEC-1: a ban kills the live access token, refresh rotation, and re-login."""
    from app.repositories.admin_read_repository import AdminReadRepository
    from app.repositories.audit_repository import AuditRepository
    from app.repositories.courier_repository import CourierRepository
    from app.repositories.order_repository import OrderRepository
    from app.repositories.promo_repository import PromoRepository
    from app.repositories.user_repository import UserRepository
    from app.services.admin_service import AdminService

    settings = _settings()
    await _skip_unless_db(settings)
    app = create_app_for_test(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            phone = _phone()
            tokens = await _register(client, app, phone)
            headers = {"Authorization": f"Bearer {tokens['access_token']}"}
            me = await client.get("/api/users/me", headers=headers)
            assert me.status_code == 200
            user_id = uuid.UUID(me.json()["id"])

            # Ban through the same service the admin dashboard uses.
            async with app.state.session_factory() as session:
                admin = User(phone=_phone(), role=UserRole.ADMIN)
                session.add(admin)
                await session.flush()
                service = AdminService(
                    reads=AdminReadRepository(session),
                    users=UserRepository(session),
                    couriers=CourierRepository(session),
                    orders=OrderRepository(session),
                    promos=PromoRepository(session),
                    audit=AuditRepository(session),
                    auth_repo=AuthRepository(session),
                    redis=app.state.redis,
                    settings=settings,
                )
                await service.set_user_banned(
                    admin_id=admin.id, user_id=user_id, banned=True, ip=None
                )
                await session.commit()

            # The live access token dies immediately (Redis ban flag).
            dead = await client.get("/api/users/me", headers=headers)
            assert dead.status_code == 401

            # The refresh token no longer mints new tokens (family revoked + status).
            refreshed = await client.post(
                "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            )
            assert refreshed.status_code == 401

            # A fresh OTP login is refused too.
            await client.post("/api/auth/send-otp", json={"phone": phone})
            otp = app.state.clients.sms.last_otp[phone]
            relogin = await client.post("/api/auth/verify-otp", json={"phone": phone, "otp": otp})
            assert relogin.status_code == 401
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()


async def test_otp_hmac_key_prefers_dedicated_env_var() -> None:
    """SEC-3: OTP_HMAC_KEY wins; the fallback chain never lands on a constant."""
    from app.integrations.sms.fake import FakeSmsClient

    dedicated = _settings(OTP_HMAC_KEY="dedicated-otp-hmac-key-0000000000000000")
    svc = OtpService(None, FakeSmsClient(dedicated.ENVIRONMENT), dedicated)  # type: ignore[arg-type]
    assert svc._hmac_key == "dedicated-otp-hmac-key-0000000000000000"  # noqa: SLF001

    fallback = _settings()
    svc2 = OtpService(None, FakeSmsClient(fallback.ENVIRONMENT), fallback)  # type: ignore[arg-type]
    assert svc2._hmac_key == fallback.JWT_SECRET.get_secret_value()  # type: ignore[union-attr]  # noqa: SLF001


async def test_chunked_body_without_length_is_rejected() -> None:
    """SEC-7: a chunked request that declares no Content-Length gets a 411."""
    settings = _settings()
    await _skip_unless_db(settings)
    app = create_app_for_test(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:

            async def _stream():  # noqa: ANN202
                yield b'{"phone": "+966500000000"}'

            resp = await client.post("/api/auth/send-otp", content=_stream())
            assert resp.status_code == 411
            assert resp.json()["error"]["code"] == "LENGTH_REQUIRED"
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()


async def test_purge_refresh_tokens_deletes_only_long_expired() -> None:
    """PERF-3: rows expired past retention are deleted; fresh rows survive."""
    settings = _settings()
    await _skip_unless_db(settings)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        now = datetime.now(UTC)
        async with factory() as session:
            user = User(phone=_phone(), role=UserRole.CUSTOMER)
            session.add(user)
            await session.flush()
            old = RefreshToken(
                user_id=user.id,
                token_hash=_secrets.token_hex(32),
                family_id=uuid.uuid4(),
                expires_at=now - timedelta(days=settings.REFRESH_TOKEN_RETENTION_DAYS + 5),
            )
            fresh = RefreshToken(
                user_id=user.id,
                token_hash=_secrets.token_hex(32),
                family_id=uuid.uuid4(),
                expires_at=now + timedelta(days=1),
            )
            session.add_all([old, fresh])
            await session.commit()
            old_id, fresh_id = old.id, fresh.id

        deleted = await purge_refresh_tokens(factory=factory, settings=settings)
        assert deleted >= 1

        async with factory() as session:
            assert await session.get(RefreshToken, old_id) is None
            assert await session.get(RefreshToken, fresh_id) is not None
    finally:
        await engine.dispose()


def create_app_for_test(settings: Settings):  # noqa: ANN201
    """Build the app (helper so the import stays in one place)."""
    from app.main import create_app

    return create_app(settings)
