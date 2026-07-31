"""Phase 14 hardening: rate limiting, body-size guard, and the readiness probe."""

from __future__ import annotations

import os
import uuid

import pytest
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.core.jwt import create_access_token
from app.core.ratelimit import RateLimiter
from app.core.redis import build_redis
from app.main import create_app
from app.models import User
from sqlalchemy import select

from tests.conftest import make_test_settings


def _settings(**overrides: object) -> Settings:
    if os.environ.get("DATABASE_URL"):
        overrides.setdefault("DATABASE_URL", os.environ["DATABASE_URL"])
    if os.environ.get("REDIS_URL"):
        overrides.setdefault("REDIS_URL", os.environ["REDIS_URL"])
    return make_test_settings(**overrides)


async def _skip_unless_db(settings: Settings) -> None:
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as s:
            await s.execute(select(User.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    finally:
        await engine.dispose()


class _BoomRedis:
    """A Redis stand-in whose every call raises, to exercise the fail-open path."""

    async def eval(self, *args: object) -> int:
        raise RuntimeError("redis down")


class _GuardRedis:
    """A Redis script stand-in for the combined WS guard."""

    def __init__(self, result: int) -> None:
        self.result = result
        self.args: tuple[object, ...] = ()

    async def eval(self, *args: object) -> int:
        self.args = args
        return self.result


async def test_rate_limiter_allows_then_blocks() -> None:
    settings = _settings()
    redis = build_redis(settings)
    try:
        await redis.ping()
    except Exception as exc:  # noqa: BLE001
        await redis.aclose()
        pytest.skip(f"redis unavailable: {exc}")

    limiter = RateLimiter(redis, max_requests=3, window_seconds=60)
    identity = f"test:{uuid.uuid4()}"  # unique key so the shared Redis stays hermetic
    try:
        for _ in range(3):
            decision = await limiter.check(identity)
            assert decision.allowed is True
        blocked = await limiter.check(identity)
        assert blocked.allowed is False
        assert 0 < blocked.retry_after_seconds <= 60
    finally:
        await redis.delete(f"ratelimit:{identity}")
        await redis.aclose()


async def test_rate_limiter_fails_open_on_backend_error() -> None:
    limiter = RateLimiter(_BoomRedis(), max_requests=1, window_seconds=60)  # type: ignore[arg-type]
    decision = await limiter.check("whoever")
    # A Redis outage must never take the API down: the request is allowed through.
    assert decision.allowed is True
    assert decision.retry_after_seconds == 0


async def test_guarded_rate_limiter_checks_ban_and_window_in_one_round_trip() -> None:
    redis = _GuardRedis(-1)
    limiter = RateLimiter(redis, max_requests=3, window_seconds=60)  # type: ignore[arg-type]
    decision = await limiter.check_guarded("ws:user", blocked_key="auth:banned:user")

    assert decision.blocked is True
    assert decision.allowed is False
    assert redis.args[1:4] == (2, "ratelimit:ws:user", "auth:banned:user")


async def test_guarded_rate_limiter_fails_closed_on_backend_error() -> None:
    limiter = RateLimiter(_BoomRedis(), max_requests=1, window_seconds=60)  # type: ignore[arg-type]
    decision = await limiter.check_guarded("ws:user", blocked_key="auth:banned:user")

    assert decision.blocked is True
    assert decision.allowed is False


async def test_rate_limit_middleware_returns_429_with_retry_after() -> None:
    from httpx import ASGITransport, AsyncClient

    settings = _settings(
        RATE_LIMIT_ENABLED=True, RATE_LIMIT_MAX_REQUESTS=3, RATE_LIMIT_WINDOW_SECONDS=60
    )
    await _skip_unless_db(settings)
    app = create_app(settings)
    # A token for a random id gives this test its own throttle bucket (user:<uuid>),
    # isolated from every other test sharing this Redis.
    token, _jti, _ttl = create_access_token(settings, user_id=uuid.uuid4(), role="CUSTOMER")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            statuses = [
                (await client.get("/api/users/me", headers=headers)).status_code for _ in range(4)
            ]
            assert statuses[:3].count(429) == 0
            last = await client.get("/api/users/me", headers=headers)
            assert last.status_code == 429
            assert int(last.headers["Retry-After"]) > 0
            assert last.json()["error"]["code"] == "RATE_LIMITED"
            # The security-header stamp still wraps a throttled response.
            assert last.headers["X-Content-Type-Options"] == "nosniff"
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()


async def test_body_size_guard_rejects_oversized_request() -> None:
    from httpx import ASGITransport, AsyncClient

    settings = _settings(MAX_REQUEST_BODY_BYTES=10)
    await _skip_unless_db(settings)
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            resp = await client.post("/api/auth/send-otp", json={"phone": "+966500000000"})
            assert resp.status_code == 413
            assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()


async def test_readiness_probe_reports_dependencies() -> None:
    from httpx import ASGITransport, AsyncClient

    settings = _settings()
    await _skip_unless_db(settings)
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            resp = await client.get("/api/health/ready")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "ready"
            assert body["checks"] == {"database": "ok", "redis": "ok"}
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()


async def test_readiness_probe_is_not_throttled() -> None:
    from httpx import ASGITransport, AsyncClient

    settings = _settings(
        RATE_LIMIT_ENABLED=True, RATE_LIMIT_MAX_REQUESTS=1, RATE_LIMIT_WINDOW_SECONDS=60
    )
    await _skip_unless_db(settings)
    app = create_app(settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            for _ in range(3):
                resp = await client.get("/api/health/ready")
                assert resp.status_code == 200
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()
