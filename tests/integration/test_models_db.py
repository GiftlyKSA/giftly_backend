"""Database-backed tests for the schema, constraints, and triggers.

These run against the migrated database (``alembic upgrade head`` in CI) and exercise
the real CHECK constraints, partial unique indexes, PostGIS geometry, and the
append-only / freeze triggers — the things a mocked DB would test nothing of.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from app.models import Invoice, InvoiceItem, Order, Promo, Transaction, User, Wallet
from app.models.enums import (
    InvoiceStatus,
    OrderStatus,
    PromoDiscountType,
    TransactionStatus,
    TransactionType,
    UserRole,
    WalletType,
)
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _future_date() -> date:
    return date.today() + timedelta(days=30)


async def _make_customer(session: AsyncSession) -> User:
    user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    session.add(user)
    await session.flush()
    return user


async def test_system_wallets_are_seeded(db_session: AsyncSession) -> None:
    types = await db_session.scalars(select(Wallet.type).where(Wallet.user_id.is_(None)))
    seeded = set(types)
    assert {
        WalletType.SYSTEM_ESCROW,
        WalletType.SYSTEM_REVENUE,
        WalletType.SYSTEM_GATEWAY,
        WalletType.SYSTEM_TAX_PAYABLE,
    } <= seeded


async def test_order_invoice_roundtrip(db_session: AsyncSession) -> None:
    customer = await _make_customer(db_session)
    order = Order(
        customer_id=customer.id,
        delivery_city="Jeddah",
        delivery_location=WKTElement("POINT(39.1728 21.5433)", srid=4326),
        delivery_date=_future_date(),
        status=OrderStatus.NEW,
    )
    db_session.add(order)
    await db_session.flush()

    courier = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.COURIER)
    db_session.add(courier)
    await db_session.flush()

    invoice = Invoice(
        order_id=order.id,
        issued_by_courier_id=courier.id,
        status=InvoiceStatus.DRAFT,
        items_net_amount=Decimal("500.00"),
        courier_fee_amount=Decimal("100.00"),
        service_fee_amount=Decimal("30.00"),
        discount_amount=Decimal("0.00"),
        net_after_discount_amount=Decimal("630.00"),
        tax_amount=Decimal("94.50"),
        total_amount=Decimal("724.50"),
    )
    db_session.add(invoice)
    await db_session.flush()

    item = InvoiceItem(
        invoice_id=invoice.id,
        position=1,
        title="Hand-painted ceramic vase",
        unit_price_amount=Decimal("400.00"),
        quantity=1,
        tax_rate=Decimal("0.1500"),
        line_net_amount=Decimal("400.00"),
        line_discount_amount=Decimal("0.00"),
        line_taxable_amount=Decimal("400.00"),
        line_tax_amount=Decimal("60.00"),
        line_total_amount=Decimal("460.00"),
    )
    db_session.add(item)
    await db_session.flush()

    fetched = await db_session.scalar(select(Invoice).where(Invoice.id == invoice.id))
    assert fetched is not None
    assert fetched.total_amount == Decimal("724.50")


async def test_invoice_net_math_check_rejects_bad_totals(db_session: AsyncSession) -> None:
    customer = await _make_customer(db_session)
    order = Order(
        customer_id=customer.id,
        delivery_city="Riyadh",
        delivery_location=WKTElement("POINT(46.6753 24.7136)", srid=4326),
        delivery_date=_future_date(),
    )
    db_session.add(order)
    await db_session.flush()
    bad = Invoice(
        order_id=order.id,
        issued_by_courier_id=customer.id,
        status=InvoiceStatus.ISSUED,
        items_net_amount=Decimal("500.00"),
        net_after_discount_amount=Decimal("999.00"),  # violates chk_invoice_net_math
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("999.00"),
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_ledger_delete_is_forbidden(db_session: AsyncSession) -> None:
    customer = await _make_customer(db_session)
    wallet = Wallet(user_id=customer.id, type=WalletType.CUSTOMER, balance=Decimal("100.00"))
    db_session.add(wallet)
    await db_session.flush()
    txn = Transaction(
        wallet_id=wallet.id,
        amount=Decimal("100.00"),
        type=TransactionType.TOPUP,
        status=TransactionStatus.SETTLED,
        correlation_id=uuid.uuid4(),
        balance_after=Decimal("100.00"),
    )
    db_session.add(txn)
    await db_session.flush()
    await db_session.delete(txn)
    with pytest.raises(DBAPIError):
        await db_session.flush()


async def test_promo_code_check_rejects_lowercase(db_session: AsyncSession) -> None:
    promo = Promo(
        code="welcome10",  # violates chk_promo_code_upper
        description="ten percent",
        discount_type=PromoDiscountType.PERCENT,
        percent_value=Decimal("10.00"),
    )
    db_session.add(promo)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_second_system_gateway_wallet_rejected(db_session: AsyncSession) -> None:
    dupe = Wallet(user_id=None, type=WalletType.SYSTEM_GATEWAY)
    db_session.add(dupe)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_geography_distance_query(db_session: AsyncSession) -> None:
    customer = await _make_customer(db_session)
    order = Order(
        customer_id=customer.id,
        delivery_city="Jeddah",
        delivery_location=WKTElement("POINT(39.17290 21.54340)", srid=4326),
        delivery_date=_future_date(),
    )
    db_session.add(order)
    await db_session.flush()
    # ST_Distance on geography returns metres; the courier is ~19m away.
    meters = await db_session.scalar(
        select(
            func.ST_Distance(
                func.cast(Order.delivery_location, __import__("geoalchemy2").Geography),
                func.cast(
                    func.ST_SetSRID(func.ST_MakePoint(39.17280, 21.54325), 4326),
                    __import__("geoalchemy2").Geography,
                ),
            )
        ).where(Order.id == order.id)
    )
    assert meters is not None
    assert meters < 200
