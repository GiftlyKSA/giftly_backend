"""Environment-backed admin dashboard authentication and server-side sessions.

The username and password remain process environment secrets; the database stores only
a stable internal actor for foreign keys/audit attribution and hashes of random session
tokens. Sessions slide up to an absolute 12-hour cap. Sensitive actions require recent
password step-up verification.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.exceptions import ForbiddenError, RateLimitedError, UnauthorizedError
from app.core.security import generate_session_token, make_csrf_token, sha256_hex
from app.models import AdminSession, User
from app.models.enums import UserRole, UserStatus
from app.repositories.admin_session_repository import AdminSessionRepository
from app.repositories.user_repository import UserRepository

_ABSOLUTE_CAP = timedelta(hours=12)
_STEPUP_TTL_SECONDS = 300
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_THROTTLE_LUA = """
for i, key in ipairs(KEYS) do
    local count = redis.call('INCR', key)
    if count == 1 or redis.call('TTL', key) < 0 then
        redis.call('EXPIRE', key, ARGV[1])
    end
    if count > tonumber(ARGV[2]) then
        return 0
    end
end
return 1
"""


@dataclass(frozen=True)
class AdminLogin:
    """The result of a completed admin login."""

    raw_session_token: str
    csrf_token: str
    admin: User


class AdminAuthService:
    """Authenticates the dashboard and manages its server-side sessions."""

    def __init__(
        self,
        *,
        users: UserRepository,
        sessions: AdminSessionRepository,
        redis: Redis,
        settings: Settings,
    ) -> None:
        """Wire the collaborators the admin auth flow needs."""
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

    async def complete_login(
        self,
        *,
        username: str,
        password: str,
        ip: str | None,
        user_agent: str | None,
    ) -> AdminLogin:
        """Verify environment credentials and create an attributed DB session.

        Raises:
            UnauthorizedError: Credentials are invalid or the admin actor is disabled.
            RateLimitedError: Too many attempts targeted this username or source IP.
        """
        generic = UnauthorizedError("Login failed. Check your credentials and try again.")
        keys = await self._check_login_throttle(username, ip)
        if not self._credentials_match(username, password):
            raise generic
        user = await self._users.ensure_dashboard_admin(username)
        if user is None or user.role is not UserRole.ADMIN or user.status is not UserStatus.ACTIVE:
            raise generic
        await self._redis.delete(*keys)

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

    async def grant_step_up(
        self, *, password: str, session_token_hash: str, ip: str | None
    ) -> None:
        """Recheck the password and mark this session step-up-authorised briefly.

        Raises:
            ForbiddenError: The password did not verify.
        """
        username = self._settings.ADMIN_USERNAME or ""
        keys = await self._check_login_throttle(username, ip)
        if not self._credentials_match(username, password):
            raise ForbiddenError("Step-up verification failed.")
        await self._redis.delete(*keys)
        await self._redis.set(self._stepup_key(session_token_hash), "1", ex=_STEPUP_TTL_SECONDS)

    async def has_step_up(self, session_token_hash: str) -> bool:
        """Return whether this session currently holds a valid step-up grant."""
        return bool(await self._redis.get(self._stepup_key(session_token_hash)))

    @staticmethod
    def _stepup_key(session_token_hash: str) -> str:
        return f"admin:stepup:{session_token_hash}"

    def _credentials_match(self, username: str, password: str) -> bool:
        configured_password = self._settings.ADMIN_PASSWORD
        expected_username = self._settings.ADMIN_USERNAME or ""
        expected_password = (
            configured_password.get_secret_value() if configured_password is not None else ""
        )
        username_ok = hmac.compare_digest(
            username.encode("utf-8"), expected_username.encode("utf-8")
        )
        password_ok = hmac.compare_digest(
            password.encode("utf-8"), expected_password.encode("utf-8")
        )
        return username_ok & password_ok

    async def _check_login_throttle(self, username: str, ip: str | None) -> tuple[str, str]:
        """Limit attempts by both username and source IP, failing closed."""
        keys = (
            f"admin:login:user:{sha256_hex(username.casefold())}",
            f"admin:login:ip:{sha256_hex(ip or 'unknown')}",
        )
        try:
            allowed = bool(
                await self._redis.eval(
                    _LOGIN_THROTTLE_LUA,
                    len(keys),
                    *keys,
                    _LOGIN_WINDOW_SECONDS,
                    _LOGIN_MAX_ATTEMPTS,
                )
            )
        except Exception as exc:
            raise RateLimitedError(
                _LOGIN_WINDOW_SECONDS, "Admin authentication is temporarily unavailable."
            ) from exc
        if not allowed:
            raise RateLimitedError(_LOGIN_WINDOW_SECONDS)
        return keys
