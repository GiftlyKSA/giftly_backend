"""Device-token persistence for push notifications (SPEC SECTION 5.1, 13).

A token is unique across users (``uq_device_tokens_token``): registering a token that
already exists re-points it at the current user and refreshes ``last_seen_at``, so a
handed-down device never keeps pushing to its previous owner.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CourierProfile, DeviceToken, User
from app.models.enums import DeviceOs, UserRole, UserStatus


class DeviceTokenRepository:
    """Registers, removes, and looks up push tokens."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def register(self, *, user_id: uuid.UUID, token: str, device_os: DeviceOs) -> DeviceToken:
        """Upsert a token to this user, refreshing last_seen_at (idempotent)."""
        existing = await self._session.scalar(select(DeviceToken).where(DeviceToken.token == token))
        if existing is not None:
            existing.user_id = user_id
            existing.device_os = device_os
            existing.last_seen_at = datetime.now(UTC)
            await self._session.flush()
            return existing
        row = DeviceToken(user_id=user_id, token=token, device_os=device_os)
        self._session.add(row)
        await self._session.flush()
        return row

    async def remove(self, *, user_id: uuid.UUID, token: str) -> None:
        """Delete a token, but only if it belongs to this user (ownership in query)."""
        await self._session.execute(
            delete(DeviceToken).where(DeviceToken.token == token, DeviceToken.user_id == user_id)
        )
        await self._session.flush()

    async def tokens_for_user(self, user_id: uuid.UUID) -> list[str]:
        """Return all push tokens registered to a user."""
        rows = await self._session.scalars(
            select(DeviceToken.token).where(DeviceToken.user_id == user_id)
        )
        return list(rows)

    async def tokens_for_city_couriers(self, city: str) -> list[str]:
        """Return push tokens of ACTIVE, verified couriers based in a city."""
        rows = await self._session.scalars(
            select(DeviceToken.token)
            .join(User, User.id == DeviceToken.user_id)
            .join(CourierProfile, CourierProfile.user_id == User.id)
            .where(
                User.role == UserRole.COURIER,
                User.status == UserStatus.ACTIVE,
                CourierProfile.is_verified.is_(True),
                CourierProfile.city_of_residence == city,
            )
        )
        return list(rows)
