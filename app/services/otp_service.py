"""OTP request and verification (SPEC SECTION 17.2 A07, 20.A).

Codes are generated with a CSPRNG and stored in Redis only as an HMAC — a Redis dump
must not be a free login. Requests are rate limited per phone with a block window;
verification is single-use and capped. The admin dashboard reuses this service.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.exceptions import RateLimitedError
from app.core.security import constant_time_equals, generate_otp, hmac_hex
from app.integrations.sms.base import SmsClient


class OtpService:
    """Issues and verifies phone OTPs, throttled through Redis."""

    def __init__(self, redis: Redis, sms: SmsClient, settings: Settings) -> None:
        """Hold the Redis client, the SMS sender, and the OTP tuning from settings."""
        self._redis = redis
        self._sms = sms
        self._settings = settings
        # The OTP HMAC key: the JWT secret is a >=32-byte app secret already validated
        # at boot. The code is ephemeral (180s TTL); this protects a Redis dump.
        self._hmac_key = settings.JWT_SECRET.get_secret_value() if settings.JWT_SECRET else "otp"

    def _code_key(self, phone: str) -> str:
        return f"otp:code:{phone}"

    def _attempts_key(self, phone: str) -> str:
        return f"otp:attempts:{phone}"

    def _rate_key(self, phone: str) -> str:
        return f"otp:rate:{phone}"

    def _block_key(self, phone: str) -> str:
        return f"otp:block:{phone}"

    async def request_otp(self, phone: str) -> str | None:
        """Generate, store, and send an OTP for ``phone``.

        Returns:
            The code in development (so a developer can complete login without SMS),
            otherwise None.

        Raises:
            RateLimitedError: The phone is blocked or over its request window.
        """
        if await self._redis.get(self._block_key(phone)):
            raise RateLimitedError(self._settings.OTP_BLOCK_SECONDS)

        count = await self._redis.incr(self._rate_key(phone))
        if count == 1:
            await self._redis.expire(self._rate_key(phone), self._settings.OTP_WINDOW_SECONDS)
        if count > self._settings.OTP_MAX_PER_WINDOW:
            await self._redis.set(self._block_key(phone), "1", ex=self._settings.OTP_BLOCK_SECONDS)
            raise RateLimitedError(self._settings.OTP_BLOCK_SECONDS)

        code = generate_otp()
        await self._redis.set(
            self._code_key(phone),
            hmac_hex(code, self._hmac_key),
            ex=self._settings.OTP_TTL_SECONDS,
        )
        await self._redis.delete(self._attempts_key(phone))
        await self._sms.send_otp(phone, code)

        return code if self._settings.ENVIRONMENT.value == "development" else None

    async def verify_otp(self, phone: str, code: str) -> bool:
        """Verify a submitted OTP, consuming it on success.

        Returns:
            True on a correct, unexpired, unexhausted code; False otherwise.

        Raises:
            RateLimitedError: Too many verification attempts for this code.
        """
        attempts = await self._redis.incr(self._attempts_key(phone))
        if attempts == 1:
            await self._redis.expire(self._attempts_key(phone), self._settings.OTP_TTL_SECONDS)
        if attempts > 5:
            await self._redis.delete(self._code_key(phone))
            raise RateLimitedError(self._settings.OTP_TTL_SECONDS)

        raw: bytes | str | None = await self._redis.get(self._code_key(phone))
        if raw is None:
            return False
        stored = raw.decode() if isinstance(raw, bytes) else raw
        if not constant_time_equals(stored, hmac_hex(code, self._hmac_key)):
            return False

        await self._redis.delete(self._code_key(phone), self._attempts_key(phone))
        await self._redis.delete(self._rate_key(phone))
        return True
