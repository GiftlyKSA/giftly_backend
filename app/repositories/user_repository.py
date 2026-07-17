"""User persistence used by the admin dashboard and auth (SPEC SECTION 10)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.models.enums import UserStatus


class UserRepository:
    """Reads and status-mutates user rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        """Return a user by id, or None."""
        return await self._session.get(User, user_id)

    async def get_by_phone(self, phone: str) -> User | None:
        """Return a user by exact phone, or None."""
        result: User | None = await self._session.scalar(select(User).where(User.phone == phone))
        return result

    async def set_status(self, user: User, status: UserStatus) -> None:
        """Update a user's account status (ban/unban)."""
        user.status = status
        await self._session.flush()
