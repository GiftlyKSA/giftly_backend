"""In-memory SMS double (SPEC SECTION 5.1 / 23).

Captures OTPs so tests and local development can read them without a real provider.
In development the OTP is surfaced in the API response; in test it is captured here.
"""

from __future__ import annotations

from app.core.config import Environment
from app.integrations._guard import forbid_in_production
from app.integrations.sms.base import SmsClient


class FakeSmsClient(SmsClient):
    """Records the most recent OTP per phone in memory."""

    def __init__(self, environment: Environment) -> None:
        """Refuse construction in production, then start empty."""
        forbid_in_production(environment, type(self).__name__)
        self.last_otp: dict[str, str] = {}

    async def send_otp(self, phone: str, code: str) -> None:
        """Record the OTP instead of sending it."""
        self.last_otp[phone] = code
