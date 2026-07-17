"""The SMS/OTP client contract (SPEC SECTION 5.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class SmsClient(ABC):
    """Sends one-time-password SMS messages."""

    @abstractmethod
    async def send_otp(self, phone: str, code: str) -> None:
        """Send an OTP ``code`` to ``phone``.

        Note:
            The OTP code is Restricted and must never be logged by any implementation.
        """
