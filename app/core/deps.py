"""Shared FastAPI dependencies for authentication and authorization (SPEC §17.2 A01).

Deny by default: every route is authenticated unless explicitly public. The actor's
id and role come from the verified JWT, never the request body. Role checks are
dependencies, not scattered if-statements.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.jwt import JwtError, decode_access_token
from app.models.enums import UserRole


@dataclass(frozen=True)
class Actor:
    """The authenticated caller, derived solely from the verified access token."""

    id: uuid.UUID
    role: UserRole
    jti: str


def get_settings(request: Request) -> Settings:
    """Return the app settings from application state."""
    settings: Settings = request.app.state.settings
    return settings


def get_redis(request: Request) -> Redis:
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


async def require_auth(request: Request) -> Actor:
    """Authenticate the request from its Bearer token.

    Raises:
        UnauthorizedError: The header is missing/malformed, the token is invalid, or
            its jti has been denylisted (logout/ban).
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise UnauthorizedError("Missing bearer token.")
    token = header[len("Bearer ") :].strip()
    settings = get_settings(request)
    try:
        claims = decode_access_token(settings, token)
    except JwtError as exc:
        raise UnauthorizedError("Invalid or expired token.") from exc

    redis = get_redis(request)
    # One round trip: the logout denylist and the ban flag (audit SEC-1 — a ban must
    # kill access tokens already in the wild, not just block new logins).
    denylisted, banned = await redis.mget(f"jwt:denylist:{claims.jti}", f"auth:banned:{claims.sub}")
    if denylisted:
        raise UnauthorizedError("This session has been revoked.")
    if banned:
        raise UnauthorizedError("This account has been suspended.")

    try:
        role = UserRole(claims.role)
    except ValueError as exc:
        raise UnauthorizedError("Malformed token role.") from exc
    return Actor(id=uuid.UUID(claims.sub), role=role, jti=claims.jti)


def require_role(
    *allowed: UserRole,
) -> Callable[[Actor], Coroutine[Any, Any, Actor]]:
    """Build a dependency that requires the actor to hold one of ``allowed`` roles."""

    async def _dependency(actor: Actor = Depends(require_auth)) -> Actor:
        if actor.role not in allowed:
            raise ForbiddenError("Your role may not perform this action.")
        return actor

    return _dependency
