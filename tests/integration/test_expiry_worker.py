"""Expiry sweeper: a lapsed unpaid invoice reopens its order and releases the hold."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.models import Invoice, Order, PaymentIntent, User, Wallet
from app.models.enums import (
    InvoiceStatus,
    OrderStatus,
    PaymentIntentStatus,
    PaymentPurpose,
    TransactionType,
    UserRole,
    WalletType,
)
from app.repositories.wallet_repository import WalletRepository
from app.services.money_service import Leg, MoneyService
from app.workers.expiry import expire_stale
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


async def test_expiry_reopens_order_and_releases_hold() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as s:
            await s.execute(select(User.id).limit(1))
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
            wallet = Wallet(user_id=customer.id, type=WalletType.CUSTOMER)
            session.add(wallet)
            await session.flush()
            # Fund the wallet through the ledger (gateway -> wallet) so the balance ==
            # settled-sum invariant holds, then place a 300 hold against the pending pay.
            repo = WalletRepository(session)
            money = MoneyService(repo)
            gateway = await repo.get_system(WalletType.SYSTEM_GATEWAY)
            await money.post_group(
                correlation_id=uuid.uuid4(),
                legs=[
                    Leg(
                        wallet_id=gateway.id,
                        amount=Decimal("-1000.00"),
                        txn_type=TransactionType.TOPUP,
                    ),
                    Leg(
                        wallet_id=wallet.id,
                        amount=Decimal("1000.00"),
                        txn_type=TransactionType.TOPUP,
                    ),
                ],
            )
            await money.hold_funds(wallet_id=wallet.id, amount=Decimal("300.00"))
            order = Order(
                customer_id=customer.id,
                courier_id=courier.id,
                delivery_city="Jeddah",
                delivery_location=WKTElement("POINT(39.2 21.5)", srid=4326),
                delivery_date=datetime.now(UTC).date() + timedelta(days=5),
                status=OrderStatus.WAITING_PAYMENT,
                total_amount=Decimal("724.50"),
            )
            session.add(order)
            await session.flush()
            invoice = Invoice(
                order_id=order.id,
                issued_by_courier_id=courier.id,
                status=InvoiceStatus.ISSUED,
                items_net_amount=Decimal("500.00"),
                courier_fee_amount=Decimal("100.00"),
                service_fee_amount=Decimal("30.00"),
                discount_amount=Decimal("0.00"),
                net_after_discount_amount=Decimal("630.00"),
                tax_amount=Decimal("94.50"),
                total_amount=Decimal("724.50"),
                amount_from_wallet=Decimal("300.00"),
                amount_from_gateway=Decimal("424.50"),
                issued_at=datetime.now(UTC) - timedelta(hours=50),
                expires_at=datetime.now(UTC) - timedelta(hours=1),  # already lapsed
            )
            session.add(invoice)
            await session.flush()
            intent = PaymentIntent(
                user_id=customer.id,
                purpose=PaymentPurpose.ORDER_INVOICE,
                amount=Decimal("424.50"),
                status=PaymentIntentStatus.NEW,
                reference_invoice_id=invoice.id,
                streampay_payment_link_id=f"LINK-{uuid.uuid4().hex[:10]}",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
            session.add(intent)
            await session.commit()
            order_id, invoice_id, intent_id = order.id, invoice.id, intent.id

        invoices_expired, _intents = await expire_stale(factory=factory, settings=settings)
        assert invoices_expired >= 1

        async with factory() as session:
            inv = await session.get(Invoice, invoice_id)
            assert inv is not None and inv.status is InvoiceStatus.EXPIRED
            order = await session.get(Order, order_id)
            assert order is not None and order.status is OrderStatus.ASSIGNED
            pi = await session.get(PaymentIntent, intent_id)
            assert pi is not None and pi.status is PaymentIntentStatus.EXPIRED
            # The 300 hold was released (balance untouched, held back to 0).
            wallet = await WalletRepository(session).get_by_user(order.customer_id)
            assert wallet is not None
            assert wallet.balance == Decimal("1000.00")
            assert wallet.held_balance == Decimal("0.00")
    finally:
        await engine.dispose()


async def test_expiry_builds_own_engine_and_scheduled_task() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as s:
            await s.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")
    await engine.dispose()

    # No factory injected: the sweeper builds and disposes its own engine.
    invoices, intents = await expire_stale(settings=settings)
    assert invoices >= 0 and intents >= 0

    from app.workers.expiry import run_expire_stale

    # The scheduled entry point acquires a Redis lock, sweeps, and releases it.
    await run_expire_stale()
