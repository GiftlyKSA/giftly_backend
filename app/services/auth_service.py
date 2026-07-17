"""Authentication service: OTP -> JWT, registration, and refresh rotation.

Implements SPEC SECTION 20.A. Access tokens are 30-minute JWTs; refresh tokens are
opaque 30-day tokens stored hashed in families with reuse detection. A new phone gets
a short-lived registration token from verify-otp, not an access token. Logout
denylists the access token's ``jti`` in Redis.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import build_aad, build_cipher
from app.core.exceptions import ConflictError, UnauthorizedError, ValidationDomainError
from app.core.jwt import (
    create_access_token,
    create_registration_token,
    decode_registration_token,
)
from app.core.security import generate_session_token, hmac_hex, sha256_hex
from app.models.enums import UserRole
from app.repositories.auth_repository import AuthRepository
from app.repositories.user_repository import UserRepository
from app.services.otp_service import OtpService


@dataclass(frozen=True)
class TokenPair:
    """An issued access + refresh token pair with the caller's role."""

    access_token: str
    refresh_token: str
    role: str


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of verify-otp: either tokens, or a registration handoff."""

    is_new_user: bool
    tokens: TokenPair | None
    registration_token: str | None


class AuthService:
    """Coordinates OTP, user creation, JWT issuance, and refresh rotation."""

    def __init__(
        self,
        *,
        settings: Settings,
        redis: Redis,
        otp: OtpService,
        users: UserRepository,
        auth_repo: AuthRepository,
        session: AsyncSession,
    ) -> None:
        """Wire the collaborators the auth flows need."""
        self._settings = settings
        self._redis = redis
        self._otp = otp
        self._users = users
        self._repo = auth_repo
        self._session = session

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    async def send_otp(self, phone: str) -> tuple[int, str | None]:
        """Request an OTP; returns (expires_in_seconds, dev_code_or_None)."""
        dev_code = await self._otp.request_otp(phone)
        return self._settings.OTP_TTL_SECONDS, dev_code

    async def verify_otp(self, phone: str, code: str) -> VerifyResult:
        """Verify an OTP; issue tokens for an existing user or a registration token.

        Raises:
            UnauthorizedError: The OTP is wrong or expired.
        """
        if not await self._otp.verify_otp(phone, code):
            raise UnauthorizedError("The code is incorrect or has expired.")
        user = await self._users.get_by_phone(phone)
        if user is None:
            token = create_registration_token(self._settings, phone=phone)
            return VerifyResult(is_new_user=True, tokens=None, registration_token=token)
        tokens = await self._issue_tokens(user.id, user.role.value)
        return VerifyResult(is_new_user=False, tokens=tokens, registration_token=None)

    async def register(
        self,
        *,
        registration_token: str,
        role: UserRole,
        full_name: str | None,
        email: str | None,
        dob: date | None,
        city: str | None,
        national_id: str | None,
        passport_id: str | None,
    ) -> TokenPair:
        """Create the account authorised by a registration token and issue tokens.

        Raises:
            UnauthorizedError: The registration token is invalid or expired.
            ValidationDomainError: Required fields for the chosen role are missing.
            ConflictError: The phone or courier identity is already registered.
        """
        phone = decode_registration_token(self._settings, registration_token)
        if await self._users.get_by_phone(phone) is not None:
            raise ConflictError("This phone is already registered.")

        if role is UserRole.CUSTOMER:
            user = await self._repo.create_customer(
                phone=phone, full_name=full_name, email=email, dob=dob
            )
            return await self._issue_tokens(user.id, user.role.value)

        if role is UserRole.COURIER:
            return await self._register_courier(
                phone=phone,
                full_name=full_name,
                email=email,
                dob=dob,
                city=city,
                national_id=national_id,
                passport_id=passport_id,
            )
        raise ValidationDomainError("A public user cannot register as an admin.")

    async def _register_courier(
        self,
        *,
        phone: str,
        full_name: str | None,
        email: str | None,
        dob: date | None,
        city: str | None,
        national_id: str | None,
        passport_id: str | None,
    ) -> TokenPair:
        if not city:
            raise ValidationDomainError("A courier must provide a city of residence.")
        if not (national_id or passport_id):
            raise ValidationDomainError("A courier must provide a national id or passport.")

        raw_id = (national_id or passport_id or "").strip()
        fingerprint = hmac_hex(
            raw_id, self._settings.IDENTITY_FINGERPRINT_PEPPER.get_secret_value()
        )
        if await self._repo.fingerprint_exists(fingerprint):
            raise ConflictError("This identity document is already registered.")

        user = await self._repo.create_courier_user(
            phone=phone, full_name=full_name, email=email, dob=dob
        )
        cipher = build_cipher(
            self._settings.encryption_keys(), self._settings.FIELD_ENCRYPTION_KEY_VERSION
        )
        national_enc = (
            cipher.encrypt(
                national_id.strip(),
                build_aad("courier_profiles", "national_id", str(user.id)),
            )
            if national_id
            else None
        )
        passport_enc = (
            cipher.encrypt(
                passport_id.strip(),
                build_aad("courier_profiles", "passport_id", str(user.id)),
            )
            if passport_id
            else None
        )
        await self._repo.add_courier_wallet_and_profile(
            user_id=user.id,
            city=city,
            national_id_encrypted=national_enc,
            passport_id_encrypted=passport_enc,
            identity_fingerprint=fingerprint,
        )
        return await self._issue_tokens(user.id, user.role.value)

    async def refresh(self, raw_refresh: str) -> TokenPair:
        """Rotate a refresh token, revoking the family on reuse.

        Raises:
            UnauthorizedError: The token is unknown, expired, or already used/revoked
                (which also revokes the whole family).
        """
        token_hash = sha256_hex(raw_refresh)
        row = await self._repo.get_refresh_token(token_hash)
        now = self._now()
        if row is None:
            raise UnauthorizedError("Invalid refresh token.")
        if row.revoked_at is not None or row.used_at is not None:
            # Reuse of a rotated/revoked token: the family is compromised. Commit the
            # revoke explicitly so it survives the 401 (the request session would
            # otherwise roll it back when the exception propagates).
            await self._repo.revoke_family(row.family_id, now)
            await self._session.commit()
            raise UnauthorizedError("Refresh token reuse detected; please sign in again.")
        if row.expires_at <= now:
            raise UnauthorizedError("Refresh token has expired.")

        await self._repo.mark_refresh_used(row, now)
        user = await self._users.get(row.user_id)
        if user is None:
            raise UnauthorizedError("Account no longer exists.")
        access, _, _ = create_access_token(self._settings, user_id=user.id, role=user.role.value)
        new_refresh = await self._new_refresh(user.id, row.family_id)
        return TokenPair(access_token=access, refresh_token=new_refresh, role=user.role.value)

    async def logout(self, *, jti: str, remaining_ttl_seconds: int) -> None:
        """Denylist an access token's jti until it would have expired."""
        ttl = max(remaining_ttl_seconds, 1)
        await self._redis.set(f"jwt:denylist:{jti}", "1", ex=ttl)

    async def is_denylisted(self, jti: str) -> bool:
        """Return whether an access token's jti has been revoked."""
        return bool(await self._redis.get(f"jwt:denylist:{jti}"))

    async def _issue_tokens(self, user_id: uuid.UUID, role: str) -> TokenPair:
        access, _, _ = create_access_token(self._settings, user_id=user_id, role=role)
        refresh = await self._new_refresh(user_id, uuid.uuid4())
        return TokenPair(access_token=access, refresh_token=refresh, role=role)

    async def _new_refresh(self, user_id: uuid.UUID, family_id: uuid.UUID) -> str:
        raw = generate_session_token()
        await self._repo.add_refresh_token(
            user_id=user_id,
            token_hash=sha256_hex(raw),
            family_id=family_id,
            expires_at=self._now() + timedelta(days=self._settings.JWT_REFRESH_TTL_DAYS),
        )
        return raw
