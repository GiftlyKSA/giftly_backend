"""User persistence used by the admin dashboard and auth (SPEC SECTION 10)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.models.enums import UserRole, UserStatus

_DASHBOARD_ADMIN_NAMESPACE = uuid.UUID("48c72a54-78e4-4a0e-a20f-54378ed7f950")


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

    async def get_by_email(self, email: str) -> User | None:
        """Return a user by exact email, or None."""
        result: User | None = await self._session.scalar(select(User).where(User.email == email))
        return result

    async def create_admin_user(
        self, *, phone: str, full_name: str | None, email: str | None, role: UserRole
    ) -> User:
        """Create a customer or courier account from the dashboard."""
        user = User(phone=phone, full_name=full_name, email=email, role=role)
        self._session.add(user)
        await self._session.flush()
        return user

    async def ensure_dashboard_admin(self, username: str) -> User | None:
        """Return the stable DB actor used by environment-authenticated dashboard sessions.

        The credential remains environment-only. A reserved internal user gives sessions,
        foreign keys, and audit rows a durable actor without inventing a customer phone.
        PostgreSQL's conflict handling keeps simultaneous first logins idempotent.
        """
        admin_id = uuid.uuid5(_DASHBOARD_ADMIN_NAMESPACE, username)
        internal_phone = f"admin:{hashlib.sha256(username.encode()).hexdigest()[:14]}"
        await self._session.execute(
            insert(User)
            .values(
                id=admin_id,
                phone=internal_phone,
                full_name="Dashboard administrator",
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
            .on_conflict_do_nothing()
        )
        await self._session.flush()
        return await self.get(admin_id)

    async def update_admin_profile(
        self, user: User, *, phone: str | None, full_name: str | None, email: str | None
    ) -> None:
        """Update the safe, non-authentication fields exposed in the admin dashboard."""
        if phone is not None:
            user.phone = phone
        user.full_name = full_name
        user.email = email
        await self._session.flush()

    async def soft_delete(self, user: User) -> None:
        """Disable a user while retaining rows required for financial/audit history."""
        user.status = UserStatus.BANNED
        user.deleted_at = datetime.now(UTC)
        await self._session.flush()

    async def set_status(self, user: User, status: UserStatus) -> None:
        """Update a user's account status (ban/unban)."""
        user.status = status
        await self._session.flush()
