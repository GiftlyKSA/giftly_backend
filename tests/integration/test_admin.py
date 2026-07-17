"""End-to-end admin dashboard tests: OTP login, session, CSRF, step-up, audit.

Exercises the real session cookie, CSRF verification, step-up gating, and audit
logging against the migrated database and a real Redis, all on one event loop via
httpx's ASGI transport. Skips if either backing service is unavailable.
"""

from __future__ import annotations

import os
import secrets as _secrets

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.core.security import make_csrf_token, sha256_hex
from app.main import create_app
from app.models import AdminSession, AuditLog, Promo, User
from app.models.enums import UserRole, UserStatus
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from tests.conftest import make_test_settings

_ADMIN_SECRET = "test-admin-session-secret-not-real-00000"


def _settings() -> Settings:
    overrides: dict[str, object] = {
        "ADMIN_DASHBOARD_ENABLED": True,
        "ADMIN_SESSION_SECRET": _ADMIN_SECRET,
    }
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if os.environ.get("REDIS_URL"):
        overrides["REDIS_URL"] = os.environ["REDIS_URL"]
    return make_test_settings(**overrides)


async def _seed_admin(factory: object, phone: str) -> None:
    async with factory() as session:  # type: ignore[operator]
        session.add(User(phone=phone, role=UserRole.ADMIN, status=UserStatus.ACTIVE))
        await session.commit()


async def test_admin_full_flow() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    phone = f"+96650{_secrets.randbelow(10_000_000):07d}"
    code = f"ADMTEST{_secrets.randbelow(100000)}"

    try:
        await _seed_admin(factory, phone)
    except Exception as exc:  # noqa: BLE001 — no DB available; skip.
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")

    app = create_app(settings)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            # 1. Request the login OTP; the FakeSmsClient captures it.
            await client.post("/admin/login", data={"phone": phone})
            otp = app.state.clients.sms.last_otp[phone]

            # 2. Verify -> session cookie set, redirected to the overview.
            resp = await client.post("/admin/login/verify", data={"phone": phone, "otp": otp})
            assert resp.status_code == 303
            cookie = client.cookies.get("admin_session")
            assert cookie

            # 3. Authenticated read works.
            assert (await client.get("/admin")).status_code == 200

            csrf = make_csrf_token(sha256_hex(cookie), _ADMIN_SECRET)

            # 4. A mutation with a bad CSRF token is rejected (403).
            bad_csrf = await client.post(
                "/admin/promos",
                data={
                    "csrf_token": "not-the-real-token",
                    "code": code,
                    "description": "x",
                    "discount_type": "PERCENT",
                    "percent_value": "10.00",
                },
            )
            assert bad_csrf.status_code == 403

            # 5. With CSRF but without step-up, still rejected.
            no_stepup = await client.post(
                "/admin/promos",
                data={
                    "csrf_token": csrf,
                    "code": code,
                    "description": "x",
                    "discount_type": "PERCENT",
                    "percent_value": "10.00",
                },
            )
            assert no_stepup.status_code == 403

            # 6. Perform step-up (fresh OTP), then create the promo.
            await client.post("/admin/step-up/request", data={"next": "/admin/promos/new"})
            step_otp = app.state.clients.sms.last_otp[phone]
            step = await client.post(
                "/admin/step-up",
                data={"otp": step_otp, "next": "/admin/promos/new", "csrf_token": csrf},
            )
            assert step.status_code == 303

            created = await client.post(
                "/admin/promos",
                data={
                    "csrf_token": csrf,
                    "code": code,
                    "description": "Ten percent",
                    "discount_type": "PERCENT",
                    "percent_value": "10.00",
                    "max_discount_amount": "100.00",
                    "min_order_amount": "0.00",
                    "max_usages_per_user": "1",
                },
            )
            assert created.status_code == 303

        # 7. The promo and an audit row now exist.
        async with factory() as session:
            promo = await session.scalar(select(Promo).where(Promo.code == code.upper()))
            assert promo is not None
            audit = await session.scalar(
                select(AuditLog).where(
                    AuditLog.action == "PROMO_CREATE", AuditLog.entity_id == promo.id
                )
            )
            assert audit is not None
    finally:
        async with factory() as session:
            admin = await session.scalar(select(User).where(User.phone == phone))
            if admin is not None:
                await session.execute(delete(AuditLog).where(AuditLog.actor_user_id == admin.id))
                await session.execute(
                    delete(AdminSession).where(AdminSession.admin_user_id == admin.id)
                )
                await session.execute(delete(Promo).where(Promo.created_by_admin_id == admin.id))
                await session.execute(delete(User).where(User.id == admin.id))
            await session.commit()
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()
