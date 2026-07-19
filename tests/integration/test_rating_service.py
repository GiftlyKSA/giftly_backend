"""Rating-service branch tests: only completed orders, one per rater, aggregate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.exceptions import ConflictError, NotFoundError
from app.models import Order, User
from app.models.enums import OrderStatus, UserRole
from app.repositories.order_repository import OrderRepository
from app.repositories.rating_repository import RatingRepository
from app.services.rating_service import RatingService
from geoalchemy2 import WKTElement
from sqlalchemy.ext.asyncio import AsyncSession


def _service(db: AsyncSession) -> RatingService:
    return RatingService(orders=OrderRepository(db), ratings=RatingRepository(db))


async def _order(db: AsyncSession, status: OrderStatus) -> tuple[User, User, Order]:
    customer = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    courier = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.COURIER)
    db.add_all([customer, courier])
    await db.flush()
    order = Order(
        customer_id=customer.id,
        courier_id=courier.id,
        delivery_city="Jeddah",
        delivery_location=WKTElement("POINT(39.2 21.5)", srid=4326),
        delivery_date=datetime.now(UTC).date() + timedelta(days=5),
        status=status,
    )
    db.add(order)
    await db.flush()
    return customer, courier, order


async def test_rate_rejects_non_completed_order(db_session: AsyncSession) -> None:
    customer, _courier, order = await _order(db_session, OrderStatus.IN_PROGRESS)
    with pytest.raises(ConflictError):
        await _service(db_session).rate(
            order_id=order.id, rater_id=customer.id, score=5, comment=None
        )


async def test_rate_once_and_aggregate(db_session: AsyncSession) -> None:
    customer, courier, order = await _order(db_session, OrderStatus.COMPLETED)
    svc = _service(db_session)
    rating = await svc.rate(order_id=order.id, rater_id=customer.id, score=4, comment="good")
    assert rating.rated_user_id == courier.id

    # The same rater cannot rate twice.
    with pytest.raises(ConflictError):
        await svc.rate(order_id=order.id, rater_id=customer.id, score=1, comment=None)

    average, count = await svc.summary_for_user(courier.id)
    assert count == 1 and average == Decimal("4.00")


async def test_rate_requires_participation(db_session: AsyncSession) -> None:
    _customer, _courier, order = await _order(db_session, OrderStatus.COMPLETED)
    stranger = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    db_session.add(stranger)
    await db_session.flush()
    with pytest.raises(NotFoundError):
        await _service(db_session).rate(
            order_id=order.id, rater_id=stranger.id, score=5, comment=None
        )


async def test_summary_empty_for_unrated_user(db_session: AsyncSession) -> None:
    average, count = await _service(db_session).summary_for_user(uuid.uuid4())
    assert count == 0 and average == Decimal("0.00")
