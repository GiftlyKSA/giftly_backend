"""Security primitives: OTP codes, token hashing, and CSRF tokens (SPEC SECTION 17.2).

All randomness comes from ``secrets`` (never ``random``); every secret comparison uses
``compare_digest`` to avoid timing oracles. Tokens are stored only as SHA-256 hashes —
a database read must never be a free login.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_OTP_DIGITS = 6


def generate_otp() -> str:
    """Return a fresh 6-digit numeric OTP from a CSPRNG."""
    return f"{secrets.randbelow(10**_OTP_DIGITS):0{_OTP_DIGITS}d}"


def hmac_hex(value: str, key: str) -> str:
    """Return the HMAC-SHA256 hex digest of ``value`` under ``key``.

    Used to store OTPs and to fingerprint phone numbers — never store the plaintext.
    """
    return hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def sha256_hex(value: str) -> str:
    """Return the SHA-256 hex digest of ``value`` (for session/refresh token storage)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    """Compare two strings in constant time."""
    return hmac.compare_digest(a, b)


def generate_session_token() -> str:
    """Return 32 cryptographically-random bytes as URL-safe text (the raw cookie value)."""
    return secrets.token_urlsafe(32)


def make_csrf_token(session_token_hash: str, secret: str) -> str:
    """Derive a per-session CSRF token by signing the session hash with the app secret.

    The token is a pure function of the session and the secret, so it can be verified
    without server-side storage and rotates automatically when the session changes.
    """
    return hmac.new(
        secret.encode("utf-8"), session_token_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_csrf_token(token: str, session_token_hash: str, secret: str) -> bool:
    """Verify a CSRF token against the session hash in constant time."""
    return constant_time_equals(token, make_csrf_token(session_token_hash, secret))
