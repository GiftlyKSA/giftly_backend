"""Tests for settings boot validation and the production interlock."""

from __future__ import annotations

import base64

import pytest
from app.core.config import Settings

_ZERO_KEY_B64 = base64.b64encode(b"\x00" * 32).decode()


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "ENVIRONMENT": "test",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
        "JWT_SECRET": "x" * 40,
        "JWT_ALGORITHM": "HS256",
        "FIELD_ENCRYPTION_KEYS": f'{{"1":"{_ZERO_KEY_B64}"}}',
        "FIELD_ENCRYPTION_KEY_VERSION": "1",
        "IDENTITY_FINGERPRINT_PEPPER": "p" * 40,
        "CORS_ALLOWED_ORIGINS": "http://localhost:3000",
    }
    env.update(overrides)
    return env


def test_valid_test_settings_boot() -> None:
    settings = Settings(_env_file=None, **_base_env())  # type: ignore[call-arg]
    assert settings.ENVIRONMENT.value == "test"
    assert not settings.docs_enabled


def test_missing_environment_refuses_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear the ambient value so the absence is real (CI sets ENVIRONMENT=test).
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    env = _base_env()
    del env["ENVIRONMENT"]
    with pytest.raises(ValueError):
        Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_short_jwt_secret_refuses_boot() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(_env_file=None, **_base_env(JWT_SECRET="short"))  # type: ignore[call-arg]


def test_pepper_equal_to_key_refuses_boot() -> None:
    pepper = base64.b64decode(_ZERO_KEY_B64).decode("latin-1")
    with pytest.raises(ValueError):
        Settings(_env_file=None, **_base_env(IDENTITY_FINGERPRINT_PEPPER=pepper))  # type: ignore[call-arg]


def test_bad_encryption_key_length_refuses_boot() -> None:
    short = base64.b64encode(b"\x00" * 16).decode()
    with pytest.raises(ValueError, match="FIELD_ENCRYPTION_KEYS"):
        Settings(  # type: ignore[call-arg]
            _env_file=None, **_base_env(FIELD_ENCRYPTION_KEYS=f'{{"1":"{short}"}}')
        )


def test_production_with_debug_refuses_boot() -> None:
    with pytest.raises(ValueError, match="DEBUG"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            **_base_env(ENVIRONMENT="production", DEBUG="true"),
        )


def test_production_missing_paylink_refuses_boot() -> None:
    with pytest.raises(ValueError, match="PAYLINK"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            **_base_env(ENVIRONMENT="production", DEBUG="false"),
        )


def test_production_wildcard_cors_refuses_boot() -> None:
    with pytest.raises(ValueError, match="CORS"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            **_base_env(ENVIRONMENT="production", CORS_ALLOWED_ORIGINS="*"),
        )


def test_invalid_withdrawal_range_refuses_boot() -> None:
    with pytest.raises(ValueError, match="MAX_WITHDRAWAL_AMOUNT"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            **_base_env(MIN_WITHDRAWAL_AMOUNT="100.00", MAX_WITHDRAWAL_AMOUNT="50.00"),
        )
