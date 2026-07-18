"""HTTP tests for the wallet endpoints (GET /api/wallets/me and transactions).

Registers a customer through the real auth flow, credits their wallet through the
money service (committed), then reads the wallet and its ledger over HTTP.
"""

from __future__ import annotations

import os
import secrets as _secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.main import create_app
from app.models import PaymentIntent, User, Wallet
from app.models.enums import PaymentIntentStatus, PaymentPurpose
from app.repositories.wallet_repository import WalletRepository
from app.services.money_service import MoneyService
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


async def test_wallet_read_endpoints() -> None:
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
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            await client.post("/api/auth/send-otp", json={"phone": phone})
            otp = app.state.clients.sms.last_otp[phone]
            verify = await client.post("/api/auth/verify-otp", json={"phone": phone, "otp": otp})
            reg = verify.json()["registration_token"]
            resp = await client.post(
                "/api/auth/register", json={"registration_token": reg, "role": "CUSTOMER"}
            )
            access = resp.json()["access_token"]
            headers = {"Authorization": f"Bearer {access}"}

            # Empty wallet first.
            empty = await client.get("/api/wallets/me", headers=headers)
            assert empty.status_code == 200
            assert empty.json()["balance"] == "0.00"

            # Credit 300.00 through the money service (committed) via a real intent.
            async with factory() as s:
                user = await s.scalar(select(User).where(User.phone == phone))
                wallet = await s.scalar(select(Wallet).where(Wallet.user_id == user.id))
                intent = PaymentIntent(
                    user_id=user.id,
                    purpose=PaymentPurpose.WALLET_TOPUP,
                    amount=Decimal("300.00"),
                    status=PaymentIntentStatus.NEW,
                    expires_at=datetime.now(UTC) + timedelta(hours=48),
                )
                s.add(intent)
                await s.flush()
                await MoneyService(WalletRepository(s)).credit_topup(
                    user_wallet_id=wallet.id, amount=Decimal("300.00"), intent_id=intent.id
                )
                await s.commit()

            funded = await client.get("/api/wallets/me", headers=headers)
            assert funded.json()["balance"] == "300.00"
            assert funded.json()["available"] == "300.00"

            txns = await client.get("/api/wallets/me/transactions", headers=headers)
            assert txns.status_code == 200
            items = txns.json()["items"]
            assert len(items) == 1
            assert items[0]["amount"] == "300.00"
            assert items[0]["type"] == "TOPUP"

            # A courier/customer role is required; no token is 401.
            assert (await client.get("/api/wallets/me")).status_code == 401
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        await app.state.engine.dispose()
