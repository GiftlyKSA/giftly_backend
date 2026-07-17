"""Real SMS provider client (SPEC SECTION 5.1)."""

from __future__ import annotations

import httpx

from app.integrations.sms.base import SmsClient


class RealSmsClient(SmsClient):
    """Sends OTP SMS through the configured provider over HTTPS."""

    def __init__(self, provider_key: str, base_url: str, timeout_seconds: float = 10.0) -> None:
        """Hold the provider credentials."""
        self._provider_key = provider_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def send_otp(self, phone: str, code: str) -> None:
        """Send the OTP; the code is never logged."""
        # VENDOR CONTRACT — refine against the chosen SMS provider's API.
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/send",
                json={"to": phone, "message": f"Your SAFE-GIFT code is {code}"},
                headers={"Authorization": f"Bearer {self._provider_key}"},
            )
            response.raise_for_status()
