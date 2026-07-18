"""Redis distributed locks with safe release (SPEC SECTION 20.C, known traps).

A lock is acquired with ``SET key token NX EX ttl`` and released with a Lua
compare-and-delete on the random token — a plain ``DEL`` can delete a lock that
already expired and was re-acquired by someone else. The lock is a friction control
layered over the DB's ``SELECT ... FOR UPDATE``; both are deliberate.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

# Delete the key only if it still holds our token (atomic compare-and-delete).
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class LockNotAcquiredError(Exception):
    """Raised when a lock could not be acquired because someone else holds it."""


@asynccontextmanager
async def redis_lock(redis: Redis, key: str, *, ttl_seconds: int = 10) -> AsyncIterator[str]:
    """Acquire a Redis lock for the duration of the context, or raise.

    Args:
        redis: The async Redis client.
        key: The lock key (e.g. ``lock:order_accept:<id>``).
        ttl_seconds: Auto-expiry so a crashed holder never deadlocks the lock.

    Yields:
        The random token proving ownership.

    Raises:
        LockNotAcquiredError: The lock is already held.
    """
    token = secrets.token_hex(16)
    acquired = await redis.set(key, token, nx=True, ex=ttl_seconds)
    if not acquired:
        raise LockNotAcquiredError(key)
    try:
        yield token
    finally:
        # Compare-and-delete: only release if we still own the lock.
        await redis.eval(_RELEASE_LUA, 1, key, token)
