"""End-to-end admin dashboard tests: password login, session, CSRF, step-up, audit.

Exercises the real session cookie, CSRF verification, step-up gating, and audit
logging against the migrated database and a real Redis, all on one event loop via
httpx's ASGI transport. Skips if either backing service is unavailable.
"""

from __future__ import annotations

import hashlib
import os
import secrets as _secrets

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.core.security import make_csrf_token, sha256_hex
from app.main import create_app
from app.models import AdminSession, AuditLog, Promo, User
from app.models.enums import UserRole
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from tests.conftest import make_test_settings

_ADMIN_SECRET = "test-admin-session-secret-not-real-00000"
_ADMIN_USERNAME = f"admin-test-{_secrets.token_hex(6)}"
_ADMIN_PASSWORD = "test-admin-password"


def _settings() -> Settings:
    overrides: dict[str, object] = {
        "ADMIN_DASHBOARD_ENABLED": True,
        "ADMIN_USERNAME": _ADMIN_USERNAME,
        "ADMIN_PASSWORD": _ADMIN_PASSWORD,
        "ADMIN_SESSION_SECRET": _ADMIN_SECRET,
    }
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if os.environ.get("REDIS_URL"):
        overrides["REDIS_URL"] = os.environ["REDIS_URL"]
    return make_test_settings(**overrides)


async def test_admin_full_flow() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    code = f"ADMTEST{_secrets.randbelow(100000)}"

    try:
        async with factory() as session:
            await session.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001 — no DB available; skip.
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")

    app = create_app(settings)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            # 1. Invalid credentials fail generically and create no session.
            denied = await client.post(
                "/admin/login", data={"username": _ADMIN_USERNAME, "password": "wrong"}
            )
            assert denied.status_code == 401
            assert client.cookies.get("admin_session") is None

            # 2. Valid environment credentials create the session and DB audit actor.
            response = await client.post(
                "/admin/login",
                data={"username": _ADMIN_USERNAME, "password": _ADMIN_PASSWORD},
            )
            assert response.status_code == 303
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

            # 6. Perform password step-up, then create the promo.
            await client.post("/admin/step-up/request", data={"next": "/admin/promos/new"})
            step = await client.post(
                "/admin/step-up",
                data={
                    "password": _ADMIN_PASSWORD,
                    "next": "/admin/promos/new",
                    "csrf_token": csrf,
                },
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
            internal_phone = f"admin:{hashlib.sha256(_ADMIN_USERNAME.encode()).hexdigest()[:14]}"
            admin = await session.scalar(
                select(User).where(
                    User.phone == internal_phone,
                    User.role == UserRole.ADMIN,
                )
            )
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
