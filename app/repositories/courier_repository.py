"""Courier profile persistence used by the admin dashboard (SPEC SECTION 10, 18.3)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CourierProfile


class CourierRepository:
    """Reads courier profiles and applies verification decisions."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def get(self, user_id: uuid.UUID) -> CourierProfile | None:
        """Return a courier profile by user id, or None."""
        return await self._session.get(CourierProfile, user_id)

    async def list_pending(self, limit: int = 50) -> list[CourierProfile]:
        """Return unverified courier profiles, newest first."""
        query = (
            select(CourierProfile)
            .where(CourierProfile.is_verified.is_(False))
            .order_by(CourierProfile.created_at.desc())
            .limit(limit)
        )
        return list(await self._session.scalars(query))

    async def set_verified(
        self,
        profile: CourierProfile,
        *,
        is_verified: bool,
        admin_id: uuid.UUID,
        when: datetime,
    ) -> None:
        """Record a verification decision on a courier profile."""
        profile.is_verified = is_verified
        profile.verified_at = when if is_verified else None
        profile.verified_by_admin_id = admin_id
        await self._session.flush()
