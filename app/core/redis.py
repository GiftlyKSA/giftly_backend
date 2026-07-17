"""Async Redis client factory (SPEC SECTION 16.7).

Redis backs OTP TTLs, rate limits, distributed locks, and the admin login throttle.
The client is created once from settings and shared; callers never construct their own.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import Settings


def build_redis(settings: Settings) -> Redis:
    """Create an async Redis client from the configured URL."""
    return Redis.from_url(
        settings.REDIS_URL.get_secret_value(),
        encoding="utf-8",
        decode_responses=True,
    )
