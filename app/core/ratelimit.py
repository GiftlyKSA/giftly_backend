"""Fixed-window request rate limiting backed by Redis (SPEC SECTION 17.2 A04).

A single Redis counter per identity per window: ``INCR`` the key, set its expiry on
first hit, and reject once the count crosses the ceiling. The design mirrors the OTP
throttle so there is one throttling idiom in the codebase. It is deliberately
**fail-open**: a Redis outage must not take the whole API down, so a backend error
lets the request through (and is logged) rather than surfacing a 500.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from redis.asyncio import Redis

_logger = logging.getLogger("app.ratelimit")


@dataclass(frozen=True)
class RateLimitDecision:
    """The outcome of a rate-limit check.

    Attributes:
        allowed: Whether the request may proceed.
        retry_after_seconds: Seconds until the window resets (0 when allowed).
    """

    allowed: bool
    retry_after_seconds: int


class RateLimiter:
    """A fixed-window per-identity request throttle over Redis."""

    def __init__(self, redis: Redis, *, max_requests: int, window_seconds: int) -> None:
        """Hold the Redis client and the window tuning."""
        self._redis = redis
        self._max = max_requests
        self._window = window_seconds

    def _key(self, identity: str) -> str:
        return f"ratelimit:{identity}"

    async def check(self, identity: str) -> RateLimitDecision:
        """Count this request against ``identity`` and decide whether to allow it.

        Returns:
            An allow decision under the ceiling, otherwise a deny carrying the
            seconds until the current window expires. On any Redis error the request
            is allowed (fail-open) so a cache blip never becomes an outage.
        """
        key = self._key(identity)
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, self._window)
            if count > self._max:
                ttl = await self._redis.ttl(key)
                retry_after = ttl if isinstance(ttl, int) and ttl > 0 else self._window
                return RateLimitDecision(allowed=False, retry_after_seconds=retry_after)
            return RateLimitDecision(allowed=True, retry_after_seconds=0)
        except Exception:  # noqa: BLE001 — fail-open: availability over strict limiting.
            _logger.warning("Rate-limit backend unavailable; allowing request", exc_info=True)
            return RateLimitDecision(allowed=True, retry_after_seconds=0)
