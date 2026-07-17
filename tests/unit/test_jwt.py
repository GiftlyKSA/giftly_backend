"""Unit tests for JWT creation, verification, and algorithm pinning."""

from __future__ import annotations

import uuid

import jwt
import pytest
from app.core.jwt import (
    JwtError,
    create_access_token,
    create_registration_token,
    decode_access_token,
    decode_registration_token,
)

from tests.conftest import make_test_settings


def _settings():
    return make_test_settings()


def test_access_token_round_trip() -> None:
    settings = _settings()
    uid = uuid.uuid4()
    token, jti, ttl = create_access_token(settings, user_id=uid, role="CUSTOMER")
    claims = decode_access_token(settings, token)
    assert claims.sub == str(uid)
    assert claims.role == "CUSTOMER"
    assert claims.jti == jti
    assert ttl == settings.JWT_ACCESS_TTL_MINUTES * 60


def test_decode_rejects_wrong_secret() -> None:
    settings = _settings()
    other = make_test_settings(JWT_SECRET="a-different-secret-value-not-real-000000")
    token, _, _ = create_access_token(settings, user_id=uuid.uuid4(), role="CUSTOMER")
    with pytest.raises(JwtError):
        decode_access_token(other, token)


def test_decode_rejects_wrong_audience() -> None:
    settings = _settings()
    token = jwt.encode(
        {
            "sub": "x",
            "role": "CUSTOMER",
            "jti": "j",
            "exp": 9999999999,
            "iss": settings.JWT_ISSUER,
            "aud": "someone-else",
        },
        settings.JWT_SECRET.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(JwtError):
        decode_access_token(settings, token)


def test_registration_token_round_trip() -> None:
    settings = _settings()
    token = create_registration_token(settings, phone="+966501234567")
    assert decode_registration_token(settings, token) == "+966501234567"


def test_access_token_is_not_a_registration_token() -> None:
    settings = _settings()
    reg = create_registration_token(settings, phone="+966501234567")
    with pytest.raises(JwtError):
        decode_access_token(settings, reg)  # has purpose=registration


def test_registration_token_rejects_access_token() -> None:
    settings = _settings()
    access, _, _ = create_access_token(settings, user_id=uuid.uuid4(), role="CUSTOMER")
    with pytest.raises(JwtError):
        decode_registration_token(settings, access)
