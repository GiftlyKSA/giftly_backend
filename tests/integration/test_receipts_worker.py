"""Sweeper test: the pending-receipt job drains PAID invoices and sends each receipt once.

Uses a committing engine (the sweeper opens its own sessions and commits per invoice) and
a unique recipient address so the assertion is deterministic against shared test data.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.config import Environment, Settings
from app.core.db import build_engine, build_session_factory
from app.integrations.email.fake import FakeEmailClient
from app.models import Invoice, Order, User
from app.models.enums import InvoiceStatus, OrderStatus, UserRole
from app.workers.receipts import send_pending_receipts
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


async def test_sweeper_sends_and_stamps_receipt() -> None:
    settings = _settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    address = f"buyer-{uuid.uuid4().hex[:10]}@example.com"
    try:
        async with factory() as session:
            await session.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")

    try:
        async with factory() as session:
            customer = User(
                phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}",
                role=UserRole.CUSTOMER,
                email=address,
            )
            courier = User(
                phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.COURIER
            )
            session.add_all([customer, courier])
            await session.flush()
            order = Order(
                customer_id=customer.id,
                courier_id=courier.id,
                delivery_city="Jeddah",
                delivery_location=WKTElement("POINT(39.2 21.5)", srid=4326),
                delivery_date=datetime.now(UTC).date() + timedelta(days=10),
                status=OrderStatus.IN_PROGRESS,
            )
            session.add(order)
            await session.flush()
            invoice = Invoice(
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
            )
            session.add(invoice)
            await session.commit()
            invoice_id = invoice.id

        email = FakeEmailClient(Environment.TEST)
        sent = await send_pending_receipts(email=email, factory=factory, settings=settings)
        assert sent >= 1
        # Exactly one receipt reached our unique address.
        assert sum(1 for e in email.sent if e.to_email == address) == 1

        # The invoice is stamped, so a second sweep does not resend to us.
        async with factory() as session:
            refreshed = await session.get(Invoice, invoice_id)
            assert refreshed is not None and refreshed.receipt_email_sent_at is not None

        email2 = FakeEmailClient(Environment.TEST)
        await send_pending_receipts(email=email2, factory=factory, settings=settings)
        assert all(e.to_email != address for e in email2.sent)
    finally:
        await engine.dispose()


async def test_sweeper_builds_own_engine_when_not_injected() -> None:
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

    # No factory injected: the sweeper builds and disposes its own engine, and drains
    # the pending set with the injected fake email. It must complete without error.
    email = FakeEmailClient(Environment.TEST)
    sent = await send_pending_receipts(email=email, settings=settings)
    assert sent >= 0


async def test_scheduled_task_runs_under_lock() -> None:
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

    from app.workers.receipts import deliver_pending_receipts

    # The scheduled entry point acquires a Redis lock, sweeps, and releases it.
    await deliver_pending_receipts()
