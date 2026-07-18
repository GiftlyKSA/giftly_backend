"""Direct order-service tests for the validation, limit, and eligibility branches."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
import pytest_asyncio
from app.core.config import Environment, Settings
from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    InvalidStateTransitionError,
    ValidationDomainError,
)
from app.core.redis import build_redis
from app.integrations.storage.fake import FakeStorageClient
from app.models import CourierProfile, User
from app.models.enums import OrderStatus, UserRole, UserStatus
from app.repositories.courier_repository import CourierRepository
from app.repositories.message_repository import MessageWriter
from app.repositories.order_repository import OrderRepository
from app.repositories.user_repository import UserRepository
from app.services.media_service import MediaService
from app.services.order_service import NewOrderInput, OrderService
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_test_settings


def _settings() -> Settings:
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if os.environ.get("REDIS_URL"):
        overrides["REDIS_URL"] = os.environ["REDIS_URL"]
    return make_test_settings(**overrides)


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = build_redis(_settings())
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        pytest.skip(f"redis unavailable: {exc}")
    yield client
    await client.aclose()


def _service(db: AsyncSession, redis: Redis) -> OrderService:
    settings = _settings()
    return OrderService(
        session=db,
        orders=OrderRepository(db),
        users=UserRepository(db),
        couriers=CourierRepository(db),
        media=MediaService(FakeStorageClient(Environment.TEST), settings),
        messages=MessageWriter(db),
        redis=redis,
        settings=settings,
    )


async def _customer(db: AsyncSession) -> User:
    user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    db.add(user)
    await db.flush()
    return user


def _input(**over: object) -> NewOrderInput:
    base = {
        "description": None,
        "delivery_city": "Jeddah",
        "latitude": 21.5,
        "longitude": 39.2,
        "delivery_date": date.today() + timedelta(days=20),
        "request_media_keys": [],
    }
    base.update(over)
    return NewOrderInput(**base)  # type: ignore[arg-type]


async def test_create_rejects_out_of_range_coordinates(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    svc = _service(db_session, redis_client)
    customer = await _customer(db_session)
    with pytest.raises(ValidationDomainError):
        await svc.create_order(customer_id=customer.id, data=_input(latitude=1.0))
    with pytest.raises(ValidationDomainError):
        await svc.create_order(customer_id=customer.id, data=_input(longitude=1.0))


async def test_create_rejects_too_many_media_keys(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    svc = _service(db_session, redis_client)
    customer = await _customer(db_session)
    with pytest.raises(ValidationDomainError):
        await svc.create_order(
            customer_id=customer.id, data=_input(request_media_keys=["a", "b", "c", "d"])
        )


async def test_accept_requires_verified_active_courier(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    svc = _service(db_session, redis_client)
    customer = await _customer(db_session)
    order = await svc.create_order(customer_id=customer.id, data=_input())

    # An unverified, pending courier cannot accept.
    courier = User(
        phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}",
        role=UserRole.COURIER,
        status=UserStatus.PENDING_VERIFICATION,
    )
    db_session.add(courier)
    await db_session.flush()
    db_session.add(
        CourierProfile(
            user_id=courier.id,
            city_of_residence="Jeddah",
            is_verified=False,
            national_id_encrypted="ciphertext-placeholder",
        )
    )
    await db_session.flush()
    with pytest.raises(ForbiddenError):
        await svc.accept_order(order_id=order.id, courier_id=courier.id)


async def test_cancel_rejects_illegal_transition(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    svc = _service(db_session, redis_client)
    customer = await _customer(db_session)
    order = await svc.create_order(customer_id=customer.id, data=_input())
    # Force a non-cancellable state (IN_PROGRESS needs a courier per the CHECK).
    order.courier_id = customer.id
    order.status = OrderStatus.IN_PROGRESS
    await db_session.flush()
    with pytest.raises(InvalidStateTransitionError):
        await svc.cancel_order(order_id=order.id, actor_id=customer.id, reason=None)


async def test_create_enforces_customer_active_limit(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    svc = _service(db_session, redis_client)
    customer = await _customer(db_session)
    for _ in range(5):
        await svc.create_order(customer_id=customer.id, data=_input())
    with pytest.raises(ConflictError):
        await svc.create_order(customer_id=customer.id, data=_input())
