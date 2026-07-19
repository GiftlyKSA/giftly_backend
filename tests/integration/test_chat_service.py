"""Chat-service tests: encryption at rest, ownership, unread counters, mark-read."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio
from app.core.config import Settings
from app.core.crypto import build_aad, build_cipher
from app.core.exceptions import NotFoundError
from app.core.redis import build_redis
from app.models import Conversation, Message, Order, User
from app.models.enums import OrderStatus, UserRole
from app.repositories.chat_repository import ChatRepository
from app.services.chat_service import ChatService
from geoalchemy2 import WKTElement
from redis.asyncio import Redis
from sqlalchemy import select
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


def _service(db: AsyncSession, redis: Redis) -> ChatService:
    return ChatService(chat=ChatRepository(db), redis=redis, settings=_settings())


async def _conversation(db: AsyncSession) -> tuple[User, User, Conversation]:
    customer = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    courier = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.COURIER)
    db.add_all([customer, courier])
    await db.flush()
    order = Order(
        customer_id=customer.id,
        courier_id=courier.id,
        delivery_city="Jeddah",
        delivery_location=WKTElement("POINT(39.2 21.5)", srid=4326),
        delivery_date=date.today(),
        status=OrderStatus.IN_PROGRESS,
    )
    db.add(order)
    await db.flush()
    conv = Conversation(order_id=order.id, customer_id=customer.id, courier_id=courier.id)
    db.add(conv)
    await db.flush()
    return customer, courier, conv


async def test_message_is_encrypted_at_rest(db_session: AsyncSession, redis_client: Redis) -> None:
    customer, _courier, conv = await _conversation(db_session)
    svc = _service(db_session, redis_client)
    dto = await svc.send_message(
        conversation_id=conv.id, sender_id=customer.id, text="Meet at the blue gate"
    )
    assert dto.content == "Meet at the blue gate"

    # The stored column is ciphertext, not the plaintext.
    row = await db_session.scalar(select(Message).where(Message.id == uuid.UUID(dto.id)))
    assert row is not None
    assert row.content_encrypted != "Meet at the blue gate"
    # It decrypts only under the bound AAD.
    cipher = build_cipher(_settings().encryption_keys(), _settings().FIELD_ENCRYPTION_KEY_VERSION)
    aad = build_aad("messages", "content", str(conv.id))
    assert cipher.decrypt(row.content_encrypted, aad) == "Meet at the blue gate"


async def test_unread_counter_and_mark_read(db_session: AsyncSession, redis_client: Redis) -> None:
    customer, courier, conv = await _conversation(db_session)
    svc = _service(db_session, redis_client)
    await svc.send_message(conversation_id=conv.id, sender_id=customer.id, text="hi")
    await svc.send_message(conversation_id=conv.id, sender_id=customer.id, text="there")

    # The courier (recipient) has two unread; the customer (sender) has none.
    await db_session.refresh(conv)
    assert conv.courier_unread_count == 2
    assert conv.customer_unread_count == 0

    await svc.mark_read(conversation_id=conv.id, actor_id=courier.id)
    await db_session.refresh(conv)
    assert conv.courier_unread_count == 0


async def test_inbox_lists_decrypted_preview(db_session: AsyncSession, redis_client: Redis) -> None:
    customer, courier, conv = await _conversation(db_session)
    svc = _service(db_session, redis_client)
    await svc.send_message(conversation_id=conv.id, sender_id=courier.id, text="On my way")

    inbox = await svc.list_inbox(user_id=customer.id, limit=20, before=None)
    mine = next(i for i in inbox if i.conversation_id == str(conv.id))
    assert mine.last_message_preview == "On my way"
    assert mine.unread_count == 1
    assert mine.other_user_id == str(courier.id)


async def test_non_participant_cannot_access(db_session: AsyncSession, redis_client: Redis) -> None:
    _customer, _courier, conv = await _conversation(db_session)
    stranger = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    db_session.add(stranger)
    await db_session.flush()
    svc = _service(db_session, redis_client)
    with pytest.raises(NotFoundError):
        await svc.send_message(conversation_id=conv.id, sender_id=stranger.id, text="hello")
    with pytest.raises(NotFoundError):
        await svc.list_messages(
            conversation_id=conv.id, actor_id=stranger.id, limit=10, before_id=None
        )


async def test_list_messages_returns_full_thread(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    # Both sends share this test's single transaction, so `now()` (and thus created_at) is
    # identical; message ordering is only meaningful across transactions (one per request in
    # production). Here we assert the whole decrypted thread comes back.
    customer, courier, conv = await _conversation(db_session)
    svc = _service(db_session, redis_client)
    await svc.send_message(conversation_id=conv.id, sender_id=customer.id, text="first")
    await svc.send_message(conversation_id=conv.id, sender_id=courier.id, text="second")
    msgs = await svc.list_messages(
        conversation_id=conv.id, actor_id=customer.id, limit=10, before_id=None
    )
    assert {m.content for m in msgs} == {"first", "second"}
