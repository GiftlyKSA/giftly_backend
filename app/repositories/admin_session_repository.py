"""Admin dashboard session persistence (SPEC SECTION 18.2).

Only the SHA-256 hash of the cookie value is stored — the raw token lives in the
cookie, never the database. Sessions have a sliding TTL and an absolute cap.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminSession


class AdminSessionRepository:
    """Creates, loads, and revokes admin sessions by token hash."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def create(
        self,
        *,
        admin_user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        ip_address: str | None,
        user_agent: str | None,
    ) -> AdminSession:
        """Insert a new admin session row."""
        row = AdminSession(
            admin_user_id=admin_user_id,
            session_token_hash=token_hash,
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_active(self, token_hash: str, now: datetime) -> AdminSession | None:
        """Return the unexpired, unrevoked session for a token hash, or None."""
        query = select(AdminSession).where(
            AdminSession.session_token_hash == token_hash,
            AdminSession.revoked_at.is_(None),
            AdminSession.expires_at > now,
        )
        result: AdminSession | None = await self._session.scalar(query)
        return result

    async def touch(self, row: AdminSession, expires_at: datetime) -> None:
        """Extend a session's expiry (sliding window)."""
        row.expires_at = expires_at
        await self._session.flush()

    async def revoke(self, row: AdminSession, now: datetime) -> None:
        """Mark a session revoked (logout)."""
        row.revoked_at = now
        await self._session.flush()
