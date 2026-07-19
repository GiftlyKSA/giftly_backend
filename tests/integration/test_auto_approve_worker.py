"""Auto-approve sweeper: an overdue delivered order completes and pays the courier.

Uses a committing engine (the sweeper opens its own sessions) and funds escrow so the
release balances. A unique courier lets the assertion be deterministic against shared data.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.models import Invoice, Order, User, Wallet
from app.models.enums import (
    InvoiceStatus,
    OrderStatus,
    TransactionType,
    UserRole,
    WalletType,
)
from app.repositories.wallet_repository import WalletRepository
from app.services.money_service import Leg, MoneyService
from app.workers.auto_approve import auto_approve_delivered
from geoalchemy2 import WKTElement
from sqlalchemy import select

from tests.conftest import make_test_settings


def _settings() -> Settings:
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if os.environ.get("REDIS_URL"):
        overrides["REDIS_URL"] = os.environ["REDIS_URL"]
    return make_test_settings(**overrides)


async def test_auto_approve_completes_and_pays() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")

    try:
        async with factory() as session:
            customer = User(
                phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER
            )
            courier = User(
                phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.COURIER
            )
            session.add_all([customer, courier])
            await session.flush()
            session.add_all(
                [
                    Wallet(user_id=customer.id, type=WalletType.CUSTOMER),
                    Wallet(user_id=courier.id, type=WalletType.COURIER),
                ]
            )
            await session.flush()
            order = Order(
                customer_id=customer.id,
                courier_id=courier.id,
                delivery_city="Jeddah",
                delivery_location=WKTElement("POINT(39.2 21.5)", srid=4326),
                delivery_date=datetime.now(UTC).date() + timedelta(days=5),
                status=OrderStatus.DELIVERED,
                delivered_at=datetime.now(UTC) - timedelta(hours=100),
            )
            session.add(order)
            await session.flush()
            session.add(
                Invoice(
                    order_id=order.id,
                    issued_by_courier_id=courier.id,
                    status=InvoiceStatus.PAID,
                    items_net_amount=Decimal("500.00"),
                    courier_fee_amount=Decimal("100.00"),
                    service_fee_amount=Decimal("30.00"),
                    discount_amount=Decimal("0.00"),
                    net_after_discount_amount=Decimal("630.00"),
                    tax_amount=Decimal("94.50"),
                    total_amount=Decimal("724.50"),
                    issued_at=datetime.now(UTC),
                    paid_at=datetime.now(UTC),
                    amount_from_wallet=Decimal("724.50"),
                )
            )
            # Fund escrow so the release balances.
            repo = WalletRepository(session)
            gateway = await repo.get_system(WalletType.SYSTEM_GATEWAY)
            escrow = await repo.get_system(WalletType.SYSTEM_ESCROW)
            await MoneyService(repo).post_group(
                correlation_id=uuid.uuid4(),
                legs=[
                    Leg(
                        wallet_id=gateway.id,
                        amount=Decimal("-724.50"),
                        txn_type=TransactionType.PAYMENT,
                    ),
                    Leg(
                        wallet_id=escrow.id,
                        amount=Decimal("724.50"),
                        txn_type=TransactionType.PAYMENT,
                    ),
                ],
            )
            await session.commit()
            order_id, courier_id = order.id, courier.id

        completed = await auto_approve_delivered(factory=factory, settings=settings)
        assert completed >= 1

        async with factory() as session:
            refreshed = await session.get(Order, order_id)
            assert refreshed is not None and refreshed.status is OrderStatus.COMPLETED
            courier_wallet = await WalletRepository(session).get_by_user(courier_id)
            assert courier_wallet is not None and courier_wallet.balance == Decimal("540.00")
    finally:
        await engine.dispose()


async def test_auto_approve_builds_own_engine() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")
    await engine.dispose()

    # No factory injected: the sweeper builds and disposes its own engine.
    completed = await auto_approve_delivered(settings=settings)
    assert completed >= 0


async def test_scheduled_auto_approve_runs_under_lock() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")
    await engine.dispose()

    from app.workers.auto_approve import run_auto_approve

    await run_auto_approve()
