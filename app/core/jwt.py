"""JWT encoding/decoding with a pinned algorithm (SPEC SECTION 17.2 A07).

The expected algorithm is pinned from configuration and never read from the token's
own header — this defeats ``alg: none`` and the RS256->HS256 confusion attack. Access
tokens carry only ``sub, role, jti, iat, exp, iss, aud``; profile data is never in the
token. A separate short-lived registration token gates ``/api/auth/register``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import Settings

_REGISTRATION_TTL = timedelta(minutes=10)


class JwtError(Exception):
    """Raised when a token fails to decode, verify, or match the expected type."""


@dataclass(frozen=True)
class AccessClaims:
    """The verified claims of an access token."""

    sub: str
    role: str
    jti: str
    exp: int


def _signing_key(settings: Settings) -> str:
    if settings.JWT_ALGORITHM == "HS256":
        assert settings.JWT_SECRET is not None
        return settings.JWT_SECRET.get_secret_value()
    assert settings.JWT_PRIVATE_KEY is not None
    return settings.JWT_PRIVATE_KEY.get_secret_value()


def _verify_key(settings: Settings) -> str:
    if settings.JWT_ALGORITHM == "HS256":
        assert settings.JWT_SECRET is not None
        return settings.JWT_SECRET.get_secret_value()
    assert settings.JWT_PUBLIC_KEY is not None
    return settings.JWT_PUBLIC_KEY.get_secret_value()


def create_access_token(
    settings: Settings, *, user_id: uuid.UUID, role: str
) -> tuple[str, str, int]:
    """Create a signed access token.

    Returns:
        (token, jti, ttl_seconds) — the jti and TTL support the logout denylist.
    """
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=settings.JWT_ACCESS_TTL_MINUTES)
    jti = str(uuid.uuid4())
    payload = {
        "sub": str(user_id),
        "role": role,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    token = jwt.encode(payload, _signing_key(settings), algorithm=settings.JWT_ALGORITHM)
    return token, jti, settings.JWT_ACCESS_TTL_MINUTES * 60


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    """Verify and decode an access token, pinning the expected algorithm.

    Raises:
        JwtError: The signature, algorithm, expiry, issuer, or audience is invalid.
    """
    try:
        payload = jwt.decode(
            token,
            _verify_key(settings),
            algorithms=[settings.JWT_ALGORITHM],  # pinned; never trust the token header
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
            options={"require": ["sub", "role", "jti", "exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise JwtError(str(exc)) from exc
    if "purpose" in payload:
        raise JwtError("Not an access token.")
    return AccessClaims(
        sub=payload["sub"], role=payload["role"], jti=payload["jti"], exp=payload["exp"]
    )


def create_registration_token(settings: Settings, *, phone: str) -> str:
    """Create a short-lived token that authorises exactly one registration call."""
    now = datetime.now(UTC)
    payload = {
        "sub": phone,
        "purpose": "registration",
        "iat": int(now.timestamp()),
        "exp": int((now + _REGISTRATION_TTL).timestamp()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    return jwt.encode(payload, _signing_key(settings), algorithm=settings.JWT_ALGORITHM)


def decode_registration_token(settings: Settings, token: str) -> str:
    """Verify a registration token and return the phone it authorises.

    Raises:
        JwtError: The token is invalid, expired, or not a registration token.
    """
    try:
        payload = jwt.decode(
            token,
            _verify_key(settings),
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
        )
    except jwt.PyJWTError as exc:
        raise JwtError(str(exc)) from exc
    if payload.get("purpose") != "registration":
        raise JwtError("Not a registration token.")
    return str(payload["sub"])
