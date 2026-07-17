"""Persistence for registration and refresh tokens (SPEC SECTION 20.A, 17.2 A07)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CourierProfile, RefreshToken, User, Wallet
from app.models.enums import UserRole, UserStatus, WalletType


class AuthRepository:
    """Creates users with their wallet/profile and manages refresh tokens."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def create_customer(
        self, *, phone: str, full_name: str | None, email: str | None, dob: date | None
    ) -> User:
        """Create an ACTIVE customer and their wallet in one unit of work."""
        user = User(
            phone=phone,
            full_name=full_name,
            email=email,
            date_of_birth=dob,
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
        )
        self._session.add(user)
        await self._session.flush()
        self._session.add(Wallet(user_id=user.id, type=WalletType.CUSTOMER))
        await self._session.flush()
        return user

    async def create_courier_user(
        self, *, phone: str, full_name: str | None, email: str | None, dob: date | None
    ) -> User:
        """Create a PENDING_VERIFICATION courier user (wallet/profile added after).

        The courier's identity is encrypted with an AAD bound to the user id, so the
        user must exist before the profile can be written — hence the two-step flow.
        """
        user = User(
            phone=phone,
            full_name=full_name,
            email=email,
            date_of_birth=dob,
            role=UserRole.COURIER,
            status=UserStatus.PENDING_VERIFICATION,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def add_courier_wallet_and_profile(
        self,
        *,
        user_id: uuid.UUID,
        city: str,
        national_id_encrypted: str | None,
        passport_id_encrypted: str | None,
        identity_fingerprint: str,
    ) -> None:
        """Add the courier's wallet and encrypted identity profile."""
        self._session.add(Wallet(user_id=user_id, type=WalletType.COURIER))
        self._session.add(
            CourierProfile(
                user_id=user_id,
                city_of_residence=city,
                national_id_encrypted=national_id_encrypted,
                passport_id_encrypted=passport_id_encrypted,
                identity_fingerprint=identity_fingerprint,
            )
        )
        await self._session.flush()

    async def fingerprint_exists(self, fingerprint: str) -> bool:
        """Return whether a courier identity fingerprint is already registered."""
        found = await self._session.scalar(
            select(CourierProfile.user_id).where(CourierProfile.identity_fingerprint == fingerprint)
        )
        return found is not None

    async def add_refresh_token(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        family_id: uuid.UUID,
        expires_at: datetime,
    ) -> None:
        """Persist a hashed refresh token in a family."""
        self._session.add(
            RefreshToken(
                user_id=user_id,
                token_hash=token_hash,
                family_id=family_id,
                expires_at=expires_at,
            )
        )
        await self._session.flush()

    async def get_refresh_token(self, token_hash: str) -> RefreshToken | None:
        """Load a refresh token row by its hash."""
        result: RefreshToken | None = await self._session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result

    async def mark_refresh_used(self, row: RefreshToken, now: datetime) -> None:
        """Mark a refresh token consumed (part of rotation)."""
        row.used_at = now
        await self._session.flush()

    async def revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        """Revoke every refresh token in a family (reuse detection)."""
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await self._session.flush()
