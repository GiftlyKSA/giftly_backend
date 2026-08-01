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

    async def update_admin_profile(
        self, profile: CourierProfile, *, city_of_residence: str, bio: str | None
    ) -> None:
        """Update the courier-profile fields explicitly exposed to dashboard operators."""
        profile.city_of_residence = city_of_residence
        profile.bio = bio
        await self._session.flush()

    async def create_admin_profile(
        self,
        *,
        user_id: uuid.UUID,
        city_of_residence: str,
        bio: str | None,
        national_id_encrypted: str | None,
        passport_id_encrypted: str | None,
        identity_fingerprint: str,
    ) -> CourierProfile:
        """Create a courier profile with encrypted identity data."""
        profile = CourierProfile(
            user_id=user_id,
            city_of_residence=city_of_residence,
            bio=bio,
            national_id_encrypted=national_id_encrypted,
            passport_id_encrypted=passport_id_encrypted,
            identity_fingerprint=identity_fingerprint,
        )
        self._session.add(profile)
        await self._session.flush()
        return profile

    async def fingerprint_exists(self, fingerprint: str) -> bool:
        """Return whether an identity document is already assigned to a courier."""
        return (
            await self._session.scalar(
                select(CourierProfile.user_id).where(
                    CourierProfile.identity_fingerprint == fingerprint
                )
            )
        ) is not None

    async def delete(self, profile: CourierProfile) -> None:
        """Remove a courier profile while retaining the underlying user history."""
        await self._session.delete(profile)
        await self._session.flush()
