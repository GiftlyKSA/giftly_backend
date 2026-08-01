"""End-to-end admin dashboard tests: table browser, controlled edits, and sessions.

Exercises the real session cookie, CSRF verification, admin-visible user data, redacted
table browser, read-only table policy, and audit logging against PostgreSQL and Redis.
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
from app.models import AdminSession, AuditLog, User
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


async def test_admin_table_browser_and_controlled_edit_flow() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    internal_phone = f"admin:{hashlib.sha256(_ADMIN_USERNAME.encode()).hexdigest()[:14]}"

    try:
        async with factory() as session:
            await session.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001 - no DB available; skip.
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
            csrf = make_csrf_token(sha256_hex(cookie), _ADMIN_SECRET)

            # 3. Every application table is browsable; users expose admin-visible contacts.
            catalog = await client.get("/admin/tables")
            assert catalog.status_code == 200
            assert "Data tables" in catalog.text
            assert "Admin Sessions" in catalog.text
            users = await client.get("/admin/tables/users")
            assert users.status_code == 200
            assert internal_phone in users.text

            # 4. Promo writes have no dashboard route; the table is read-only.
            assert (await client.post("/admin/promos", data={})).status_code == 405

            async with factory() as session:
                admin = await session.scalar(select(User).where(User.phone == internal_phone))
                assert admin is not None

            # 5. An authenticated admin may update the allowed user profile with CSRF.
            updated = await client.post(
                f"/admin/users/{admin.id}/edit",
                data={
                    "csrf_token": csrf,
                    "full_name": "Dashboard operator",
                    "email": "operator@example.test",
                },
            )
            assert updated.status_code == 303

        # 6. The controlled update is persisted and audited.
        async with factory() as session:
            actor = await session.scalar(select(User).where(User.phone == internal_phone))
            assert actor is not None and actor.full_name == "Dashboard operator"
            audit = await session.scalar(
                select(AuditLog).where(
                    AuditLog.action == "USER_PROFILE_UPDATE", AuditLog.entity_id == actor.id
                )
            )
            assert audit is not None
    finally:
        async with factory() as session:
            admin = await session.scalar(select(User).where(User.phone == internal_phone))
            if admin is not None:
                await session.execute(delete(AuditLog).where(AuditLog.actor_user_id == admin.id))
                await session.execute(
                    delete(AdminSession).where(AdminSession.admin_user_id == admin.id)
                )
                await session.execute(delete(User).where(User.id == admin.id))
            await session.commit()
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()
