"""Admin dashboard authentication and sessions (SPEC SECTION 18.2).

Admins log in with phone + OTP into server-side sessions. Only role=ADMIN +
status=ACTIVE completes; every other role gets the identical generic failure (no
admin-account enumeration). Cookies hold a random token; the DB stores only its
SHA-256. Sessions slide up to an absolute 12-hour cap. Step-up re-auth (a fresh OTP)
is required before revealing Restricted data or other sensitive actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import generate_session_token, make_csrf_token, sha256_hex
from app.models import AdminSession, User
from app.models.enums import UserRole, UserStatus
from app.repositories.admin_session_repository import AdminSessionRepository
from app.repositories.user_repository import UserRepository
from app.services.otp_service import OtpService

_ABSOLUTE_CAP = timedelta(hours=12)
_STEPUP_TTL_SECONDS = 300


@dataclass(frozen=True)
class AdminLogin:
    """The result of a completed admin login."""

    raw_session_token: str
    csrf_token: str
    admin: User


class AdminAuthService:
    """Orchestrates OTP login, session lifecycle, and step-up for the dashboard."""

    def __init__(
        self,
        *,
        otp: OtpService,
        users: UserRepository,
        sessions: AdminSessionRepository,
        redis: Redis,
        settings: Settings,
    ) -> None:
        """Wire the collaborators the admin auth flow needs."""
        self._otp = otp
        self._users = users
        self._sessions = sessions
        self._redis = redis
        self._settings = settings

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _session_secret(self) -> str:
        secret = self._settings.ADMIN_SESSION_SECRET
        if secret is None:
            raise UnauthorizedError("Admin dashboard is not configured.")
        return secret.get_secret_value()

    async def request_login_otp(self, phone: str) -> str | None:
        """Send a login OTP. The response is identical whether or not an admin exists."""
        return await self._otp.request_otp(phone)

    async def complete_login(
        self, *, phone: str, code: str, ip: str | None, user_agent: str | None
    ) -> AdminLogin:
        """Verify the OTP and, only for an active admin, create a session.

        Raises:
            UnauthorizedError: The OTP is wrong, or the phone is not an active admin —
                the same generic error either way (no enumeration).
        """
        generic = UnauthorizedError("Login failed. Check the code and try again.")
        if not await self._otp.verify_otp(phone, code):
            raise generic
        user = await self._users.get_by_phone(phone)
        if user is None or user.role is not UserRole.ADMIN or user.status is not UserStatus.ACTIVE:
            raise generic

        raw = generate_session_token()
        token_hash = sha256_hex(raw)
        now = self._now()
        ttl = timedelta(minutes=self._settings.ADMIN_SESSION_TTL_MINUTES)
        await self._sessions.create(
            admin_user_id=user.id,
            token_hash=token_hash,
            expires_at=now + ttl,
            ip_address=ip,
            user_agent=user_agent,
        )
        csrf = make_csrf_token(token_hash, self._session_secret())
        return AdminLogin(raw_session_token=raw, csrf_token=csrf, admin=user)

    async def load_session(self, raw_token: str) -> tuple[AdminSession, User]:
        """Load an active session and its admin, sliding the expiry within the cap.

        Raises:
            UnauthorizedError: No active session, or the admin is no longer valid.
        """
        now = self._now()
        row = await self._sessions.get_active(sha256_hex(raw_token), now)
        if row is None:
            raise UnauthorizedError("Your session has expired. Please sign in again.")
        admin = await self._users.get(row.admin_user_id)
        if (
            admin is None
            or admin.role is not UserRole.ADMIN
            or admin.status is not UserStatus.ACTIVE
        ):
            raise UnauthorizedError("Your session is no longer valid.")

        cap = row.created_at + _ABSOLUTE_CAP
        new_expiry = min(now + timedelta(minutes=self._settings.ADMIN_SESSION_TTL_MINUTES), cap)
        await self._sessions.touch(row, new_expiry)
        return row, admin

    async def logout(self, raw_token: str) -> None:
        """Revoke the session for a cookie value, if it is active."""
        now = self._now()
        row = await self._sessions.get_active(sha256_hex(raw_token), now)
        if row is not None:
            await self._sessions.revoke(row, now)

    def csrf_token_for(self, session_token_hash: str) -> str:
        """Return the CSRF token bound to a session hash."""
        return make_csrf_token(session_token_hash, self._session_secret())

    async def grant_step_up(self, *, phone: str, code: str, session_token_hash: str) -> None:
        """Verify a fresh OTP and mark this session step-up-authorised briefly.

        Raises:
            ForbiddenError: The step-up OTP did not verify.
        """
        if not await self._otp.verify_otp(phone, code):
            raise ForbiddenError("Step-up verification failed.")
        await self._redis.set(self._stepup_key(session_token_hash), "1", ex=_STEPUP_TTL_SECONDS)

    async def has_step_up(self, session_token_hash: str) -> bool:
        """Return whether this session currently holds a valid step-up grant."""
        return bool(await self._redis.get(self._stepup_key(session_token_hash)))

    @staticmethod
    def _stepup_key(session_token_hash: str) -> str:
        return f"admin:stepup:{session_token_hash}"
