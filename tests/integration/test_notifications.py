"""Notification-service and device-token tests: best-effort push, no Restricted data."""

from __future__ import annotations

import uuid

from app.core.config import Environment
from app.integrations.push.base import PushClient
from app.integrations.push.fake import FakePushClient
from app.models import CourierProfile, User
from app.models.enums import DeviceOs, UserRole, UserStatus
from app.repositories.device_token_repository import DeviceTokenRepository
from app.services.notification_service import NotificationService
from sqlalchemy.ext.asyncio import AsyncSession


async def _user(db: AsyncSession, role: UserRole, status: UserStatus = UserStatus.ACTIVE) -> User:
    user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=role, status=status)
    db.add(user)
    await db.flush()
    return user


async def test_register_is_idempotent_and_reassigns(db_session: AsyncSession) -> None:
    repo = DeviceTokenRepository(db_session)
    a = await _user(db_session, UserRole.CUSTOMER)
    b = await _user(db_session, UserRole.CUSTOMER)
    await repo.register(user_id=a.id, token="tok-1", device_os=DeviceOs.IOS)
    await repo.register(user_id=a.id, token="tok-1", device_os=DeviceOs.IOS)  # idempotent
    assert await repo.tokens_for_user(a.id) == ["tok-1"]

    # A handed-down device re-registers to the new owner and leaves the old one.
    await repo.register(user_id=b.id, token="tok-1", device_os=DeviceOs.ANDROID)
    assert await repo.tokens_for_user(a.id) == []
    assert await repo.tokens_for_user(b.id) == ["tok-1"]


async def test_remove_only_own_token(db_session: AsyncSession) -> None:
    repo = DeviceTokenRepository(db_session)
    a = await _user(db_session, UserRole.CUSTOMER)
    b = await _user(db_session, UserRole.CUSTOMER)
    await repo.register(user_id=a.id, token="tok-a", device_os=DeviceOs.IOS)
    await repo.remove(user_id=b.id, token="tok-a")  # not b's token -> no-op
    assert await repo.tokens_for_user(a.id) == ["tok-a"]
    await repo.remove(user_id=a.id, token="tok-a")
    assert await repo.tokens_for_user(a.id) == []


async def test_notify_user_pushes_to_devices(db_session: AsyncSession) -> None:
    push = FakePushClient(Environment.TEST)
    svc = NotificationService(devices=DeviceTokenRepository(db_session), push=push)
    user = await _user(db_session, UserRole.CUSTOMER)
    await DeviceTokenRepository(db_session).register(
        user_id=user.id, token="tok-x", device_os=DeviceOs.IOS
    )
    sent = await svc.notify_user(user_id=user.id, title="New message", body="You have a message.")
    assert sent == 1
    assert len(push.sent) == 1
    assert push.sent[0].tokens == ["tok-x"]
    # No Restricted data (message text) in the body.
    assert "message" in push.sent[0].body.lower()


async def test_notify_city_couriers_targets_verified_active(db_session: AsyncSession) -> None:
    push = FakePushClient(Environment.TEST)
    svc = NotificationService(devices=DeviceTokenRepository(db_session), push=push)

    verified = await _user(db_session, UserRole.COURIER)
    db_session.add(
        CourierProfile(
            user_id=verified.id,
            city_of_residence="Jeddah",
            is_verified=True,
            national_id_encrypted="x",
        )
    )
    unverified = await _user(db_session, UserRole.COURIER)
    db_session.add(
        CourierProfile(
            user_id=unverified.id,
            city_of_residence="Jeddah",
            is_verified=False,
            national_id_encrypted="x",
        )
    )
    await db_session.flush()
    repo = DeviceTokenRepository(db_session)
    await repo.register(user_id=verified.id, token="tok-verified", device_os=DeviceOs.ANDROID)
    await repo.register(user_id=unverified.id, token="tok-unverified", device_os=DeviceOs.ANDROID)

    sent = await svc.notify_city_couriers(city="Jeddah", title="New order", body="A new order.")
    assert sent == 1  # only the verified, active courier
    assert push.sent[0].tokens == ["tok-verified"]


async def test_notify_is_best_effort_on_push_failure(db_session: AsyncSession) -> None:
    class _BoomPush(PushClient):
        async def send_push(self, tokens: list[str], title: str, body: str) -> None:
            raise RuntimeError("gateway down")

    svc = NotificationService(devices=DeviceTokenRepository(db_session), push=_BoomPush())
    user = await _user(db_session, UserRole.CUSTOMER)
    await DeviceTokenRepository(db_session).register(
        user_id=user.id, token="tok-x", device_os=DeviceOs.IOS
    )
    # A push failure is swallowed — the count still reflects targeted tokens.
    assert await svc.notify_user(user_id=user.id, title="t", body="b") == 1


async def test_notify_no_devices_is_noop(db_session: AsyncSession) -> None:
    push = FakePushClient(Environment.TEST)
    svc = NotificationService(devices=DeviceTokenRepository(db_session), push=push)
    user = await _user(db_session, UserRole.CUSTOMER)
    assert await svc.notify_user(user_id=user.id, title="t", body="b") == 0
    assert push.sent == []
