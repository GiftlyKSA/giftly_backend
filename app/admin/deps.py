"""Admin dashboard request wiring: sessions, CSRF, step-up, and service assembly.

The dashboard is a browser surface with cookies, so CSRF protection is mandatory on
every state-changing form (SPEC SECTION 18.2). Unauthenticated access raises
:class:`AdminRedirect`, which the app turns into a redirect to the login page.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ForbiddenError
from app.core.security import sha256_hex, verify_csrf_token
from app.models import AdminSession, User
from app.repositories.admin_read_repository import AdminReadRepository
from app.repositories.admin_session_repository import AdminSessionRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.courier_repository import CourierRepository
from app.repositories.promo_repository import PromoRepository
from app.repositories.user_repository import UserRepository
from app.services.admin_auth_service import AdminAuthService
from app.services.admin_service import AdminService
from app.services.otp_service import OtpService

SESSION_COOKIE = "admin_session"


class AdminRedirect(Exception):  # noqa: N818 — control-flow signal, not an error
    """Raised to send an unauthenticated admin browser to the login page."""

    def __init__(self, location: str = "/admin/login") -> None:
        """Record the redirect target."""
        super().__init__(location)
        self.location = location


@dataclass
class AdminContext:
    """Everything an authenticated admin route needs, assembled per request."""

    session_row: AdminSession
    admin: User
    db: AsyncSession
    auth: AdminAuthService
    service: AdminService
    csrf_token: str


def get_settings_from(request: Request) -> Settings:
    """Return the app's settings from application state."""
    settings: Settings = request.app.state.settings
    return settings


def get_redis_from(request: Request) -> Redis:
    """Return the shared Redis client from application state."""
    redis: Redis = request.app.state.redis
    return redis


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a database session, committing on success and rolling back on error."""
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def build_auth_service(
    db: AsyncSession, redis: Redis, settings: Settings, request: Request
) -> AdminAuthService:
    """Assemble the admin auth service for a request."""
    otp = OtpService(redis, request.app.state.clients.sms, settings)
    return AdminAuthService(
        otp=otp,
        users=UserRepository(db),
        sessions=AdminSessionRepository(db),
        redis=redis,
        settings=settings,
    )


def build_admin_service(db: AsyncSession, settings: Settings) -> AdminService:
    """Assemble the admin operations service for a request."""
    return AdminService(
        reads=AdminReadRepository(db),
        users=UserRepository(db),
        couriers=CourierRepository(db),
        promos=PromoRepository(db),
        audit=AuditRepository(db),
        settings=settings,
    )


async def require_admin(request: Request, db: AsyncSession) -> AdminContext:
    """Load the current admin context or raise :class:`AdminRedirect`.

    Raises:
        AdminRedirect: No valid session cookie.
    """
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise AdminRedirect()
    settings = get_settings_from(request)
    redis = get_redis_from(request)
    auth = build_auth_service(db, redis, settings, request)
    try:
        session_row, admin = await auth.load_session(raw)
    except Exception as exc:  # noqa: BLE001 — any auth failure means "go log in".
        raise AdminRedirect() from exc
    return AdminContext(
        session_row=session_row,
        admin=admin,
        db=db,
        auth=auth,
        service=build_admin_service(db, settings),
        csrf_token=auth.csrf_token_for(session_row.session_token_hash),
    )


def verify_csrf(ctx: AdminContext, submitted_token: str, settings: Settings) -> None:
    """Verify a submitted CSRF token against the session, or raise 403.

    Raises:
        ForbiddenError: The token is missing or does not match.
    """
    secret = settings.ADMIN_SESSION_SECRET
    if secret is None or not submitted_token:
        raise ForbiddenError("Invalid CSRF token.")
    if not verify_csrf_token(
        submitted_token, ctx.session_row.session_token_hash, secret.get_secret_value()
    ):
        raise ForbiddenError("Invalid CSRF token.")


async def require_step_up(ctx: AdminContext) -> None:
    """Ensure the session holds a valid step-up grant, or raise 403.

    Raises:
        ForbiddenError: No current step-up grant.
    """
    if not await ctx.auth.has_step_up(ctx.session_row.session_token_hash):
        raise ForbiddenError("This action requires step-up re-authentication.")


def client_ip(request: Request) -> str | None:
    """Return the client IP, if the ASGI server provided one."""
    return request.client.host if request.client else None


def session_hash_from_cookie(request: Request) -> str | None:
    """Return the SHA-256 of the session cookie value, if present."""
    raw = request.cookies.get(SESSION_COOKIE)
    return sha256_hex(raw) if raw else None
