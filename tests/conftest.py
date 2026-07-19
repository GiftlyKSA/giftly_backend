"""Shared test fixtures (SPEC SECTION 23).

ENVIRONMENT=test is set here via the settings fixture, never by a developer's shell.
In test mode the client factory returns Fakes, so the suite cannot reach Paylink or
sndr.sh even if it tried.
"""

from __future__ import annotations

import base64

import pytest
from app.core.config import Environment, Settings

_ZERO_KEY_B64 = base64.b64encode(b"\x00" * 32).decode()


def make_test_settings(**overrides: object) -> Settings:
    """Construct a valid ENVIRONMENT=test Settings object with sensible dummies."""
    env: dict[str, object] = {
        "ENVIRONMENT": "test",
        "DEBUG": False,
        "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres",
        "REDIS_URL": "redis://localhost:6379/0",
        "JWT_SECRET": "test-jwt-secret-value-not-real-000000000000",
        "JWT_ALGORITHM": "HS256",
        "FIELD_ENCRYPTION_KEYS": f'{{"1":"{_ZERO_KEY_B64}"}}',
        "FIELD_ENCRYPTION_KEY_VERSION": 1,
        "IDENTITY_FINGERPRINT_PEPPER": "test-pepper-value-not-real-0000000000000",
        "ADMIN_SESSION_SECRET": "test-admin-session-secret-not-real-00000",
        "CORS_ALLOWED_ORIGINS": "http://localhost:3000",
        # The suite shares one Redis, so every unauthenticated request lands in the same
        # ip:unknown throttle bucket; leave the limiter off by default and let the
        # hardening tests opt in explicitly.
        "RATE_LIMIT_ENABLED": False,
    }
    env.update(overrides)
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


@pytest.fixture
def test_settings() -> Settings:
    """A validated test-mode settings object."""
    return make_test_settings()


@pytest.fixture
def dev_settings() -> Settings:
    """A validated development-mode settings object."""
    return make_test_settings(ENVIRONMENT=Environment.DEVELOPMENT.value)
