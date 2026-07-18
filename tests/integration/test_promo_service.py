"""Tests for the promo engine (SPEC SECTION 12, 24).

Covers validation error codes, the golden discount, the reserve/consume/release
lifecycle, and the mandated concurrency test: 50 parallel reservations against a cap of
20 yield exactly 20 reservations and 30 PROMO_USAGE_EXCEEDED, then a release returns a
slot to the pool.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.core.exceptions import (
    PromoExpiredError,
    PromoInactiveError,
    PromoMinOrderNotMetError,
    PromoNotFoundError,
    PromoNotStartedError,
    PromoUsageExceededError,
    PromoUserLimitReachedError,
)
from app.models import Invoice, Order, Promo, User
from app.models.enums import (
    InvoiceStatus,
    OrderStatus,
    PromoDiscountType,
    PromoRedemptionStatus,
    UserRole,
)
from app.repositories.promo_repository import PromoRepository
from app.services.promo_service import PromoService
from geoalchemy2.elements import WKTElement
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import make_test_settings


def _settings() -> Settings:
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if os.environ.get("REDIS_URL"):
        overrides["REDIS_URL"] = os.environ["REDIS_URL"]
    return make_test_settings(**overrides)


async def _customer(db: AsyncSession) -> User:
    user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    db.add(user)
    await db.flush()
    return user


async def _order_and_invoice(db: AsyncSession, customer_id: uuid.UUID) -> tuple[Order, Invoice]:
    order = Order(
        customer_id=customer_id,
        courier_id=customer_id,  # a self-reference is fine for these promo tests
        delivery_city="Jeddah",
        delivery_location=WKTElement("POINT(39.1728 21.5433)", srid=4326),
        delivery_date=date.today() + timedelta(days=30),
        status=OrderStatus.ASSIGNED,
    )
    db.add(order)
    await db.flush()
    invoice = Invoice(
        order_id=order.id, issued_by_courier_id=customer_id, status=InvoiceStatus.DRAFT
    )
    db.add(invoice)
    await db.flush()
    return order, invoice


def _promo(**overrides: object) -> Promo:
    base: dict[str, object] = {
        "code": f"WEL{uuid.uuid4().hex[:6].upper()}",
        "description": "ten percent",
        "discount_type": PromoDiscountType.PERCENT,
        "percent_value": Decimal("10.00"),
        "max_discount_amount": Decimal("100.00"),
        "min_order_amount": Decimal("0.00"),
        "max_usages_per_user": 1,
    }
    base.update(overrides)
    return Promo(**base)  # type: ignore[arg-type]


async def test_validate_golden_discount(db_session: AsyncSession) -> None:
    service = PromoService(PromoRepository(db_session))
    customer = await _customer(db_session)
    promo = _promo()
    db_session.add(promo)
    await db_session.flush()
    result = await service.validate(
        code=promo.code.lower(),  # case-insensitive
        discountable_base=Decimal("600.00"),
        user_id=customer.id,
    )
    assert result.discount_amount == Decimal("60.00")


async def test_validate_not_found(db_session: AsyncSession) -> None:
    service = PromoService(PromoRepository(db_session))
    customer = await _customer(db_session)
    with pytest.raises(PromoNotFoundError):
        await service.validate(
            code="NOPE", discountable_base=Decimal("100.00"), user_id=customer.id
        )


async def test_validate_inactive(db_session: AsyncSession) -> None:
    service = PromoService(PromoRepository(db_session))
    customer = await _customer(db_session)
    promo = _promo(is_active=False)
    db_session.add(promo)
    await db_session.flush()
    with pytest.raises(PromoInactiveError):
        await service.validate(
            code=promo.code, discountable_base=Decimal("100.00"), user_id=customer.id
        )


async def test_validate_window_and_min_order(db_session: AsyncSession) -> None:
    service = PromoService(PromoRepository(db_session))
    customer = await _customer(db_session)
    now = datetime.now(UTC)

    not_started = _promo(starts_at=now + timedelta(hours=1))
    db_session.add(not_started)
    await db_session.flush()
    with pytest.raises(PromoNotStartedError):
        await service.validate(
            code=not_started.code, discountable_base=Decimal("100.00"), user_id=customer.id
        )

    expired = _promo(starts_at=now - timedelta(hours=2), ends_at=now - timedelta(hours=1))
    db_session.add(expired)
    await db_session.flush()
    with pytest.raises(PromoExpiredError):
        await service.validate(
            code=expired.code, discountable_base=Decimal("100.00"), user_id=customer.id
        )

    min_order = _promo(min_order_amount=Decimal("500.00"))
    db_session.add(min_order)
    await db_session.flush()
    with pytest.raises(PromoMinOrderNotMetError):
        await service.validate(
            code=min_order.code, discountable_base=Decimal("100.00"), user_id=customer.id
        )


async def test_reserve_consume_and_release_lifecycle(db_session: AsyncSession) -> None:
    repo = PromoRepository(db_session)
    service = PromoService(repo)
    customer = await _customer(db_session)
    order, invoice = await _order_and_invoice(db_session, customer.id)
    promo = _promo(max_total_usages=5)
    db_session.add(promo)
    await db_session.flush()

    await service.reserve(
        promo=promo,
        user_id=customer.id,
        invoice_id=invoice.id,
        order_id=order.id,
        discount_amount=Decimal("60.00"),
    )
    await db_session.refresh(promo)
    assert promo.used_count == 1
    redemption = await repo.get_redemption_by_invoice(invoice.id)
    assert redemption is not None and redemption.status is PromoRedemptionStatus.RESERVED

    # Consume on payment.
    await service.consume(invoice_id=invoice.id)
    redemption = await repo.get_redemption_by_invoice(invoice.id)
    assert redemption is not None and redemption.status is PromoRedemptionStatus.CONSUMED


async def test_release_returns_slot(db_session: AsyncSession) -> None:
    repo = PromoRepository(db_session)
    service = PromoService(repo)
    customer = await _customer(db_session)
    order, invoice = await _order_and_invoice(db_session, customer.id)
    promo = _promo(max_total_usages=1)
    db_session.add(promo)
    await db_session.flush()

    await service.reserve(
        promo=promo,
        user_id=customer.id,
        invoice_id=invoice.id,
        order_id=order.id,
        discount_amount=Decimal("60.00"),
    )
    await db_session.refresh(promo)
    assert promo.used_count == 1

    await service.release(invoice_id=invoice.id)
    await db_session.refresh(promo)
    assert promo.used_count == 0  # slot returned to the pool
    redemption = await repo.get_redemption_by_invoice(invoice.id)
    assert redemption is not None and redemption.status is PromoRedemptionStatus.RELEASED


async def test_reserve_rejects_when_global_cap_exhausted(db_session: AsyncSession) -> None:
    repo = PromoRepository(db_session)
    service = PromoService(repo)
    promo = _promo(max_total_usages=1)
    db_session.add(promo)
    await db_session.flush()

    c1 = await _customer(db_session)
    o1, i1 = await _order_and_invoice(db_session, c1.id)
    await service.reserve(
        promo=promo,
        user_id=c1.id,
        invoice_id=i1.id,
        order_id=o1.id,
        discount_amount=Decimal("1.00"),
    )
    c2 = await _customer(db_session)
    o2, i2 = await _order_and_invoice(db_session, c2.id)
    with pytest.raises(PromoUsageExceededError):
        await service.reserve(
            promo=promo,
            user_id=c2.id,
            invoice_id=i2.id,
            order_id=o2.id,
            discount_amount=Decimal("1.00"),
        )


async def test_reserve_rejects_per_user_limit(db_session: AsyncSession) -> None:
    repo = PromoRepository(db_session)
    service = PromoService(repo)
    promo = _promo(max_total_usages=10, max_usages_per_user=1)
    db_session.add(promo)
    await db_session.flush()
    customer = await _customer(db_session)
    o1, i1 = await _order_and_invoice(db_session, customer.id)
    await service.reserve(
        promo=promo,
        user_id=customer.id,
        invoice_id=i1.id,
        order_id=o1.id,
        discount_amount=Decimal("1.00"),
    )
    o2, i2 = await _order_and_invoice(db_session, customer.id)
    with pytest.raises(PromoUserLimitReachedError):
        await service.reserve(
            promo=promo,
            user_id=customer.id,
            invoice_id=i2.id,
            order_id=o2.id,
            discount_amount=Decimal("1.00"),
        )


# --- Concurrency: 50 parallel reservations against a cap of 20 --------------


@pytest_asyncio.fixture
async def committing_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = _settings()
    engine = build_engine(settings)
    try:
        async with engine.connect() as conn:
            await conn.rollback()
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")
    yield build_session_factory(engine)
    await engine.dispose()


async def test_first_20_concurrency(
    committing_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Seed a committed promo capped at 20, plus 50 distinct (customer, order, invoice).
    async with committing_factory() as s:
        promo = _promo(max_total_usages=20, max_usages_per_user=1)
        s.add(promo)
        await s.flush()
        promo_id = promo.id
        triples: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
        for _ in range(50):
            customer = await _customer(s)
            order, invoice = await _order_and_invoice(s, customer.id)
            triples.append((customer.id, order.id, invoice.id))
        await s.commit()

    async def attempt(user_id: uuid.UUID, order_id: uuid.UUID, invoice_id: uuid.UUID) -> bool:
        async with committing_factory() as session:
            repo = PromoRepository(session)
            service = PromoService(repo)
            promo = await repo.get(promo_id)
            assert promo is not None
            try:
                await service.reserve(
                    promo=promo,
                    user_id=user_id,
                    invoice_id=invoice_id,
                    order_id=order_id,
                    discount_amount=Decimal("1.00"),
                )
                await session.commit()
                return True
            except PromoUsageExceededError:
                await session.rollback()
                return False

    results = await asyncio.gather(*[attempt(*t) for t in triples])
    assert sum(1 for r in results if r) == 20
    assert sum(1 for r in results if not r) == 30

    async with committing_factory() as s:
        promo = await PromoRepository(s).get(promo_id)
        assert promo is not None
        assert promo.used_count == 20
