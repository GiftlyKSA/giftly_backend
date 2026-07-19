"""Push notifications (SPEC SECTION 5.1, 13).

Notifications are BEST-EFFORT: a push failure is logged and swallowed so it never breaks
the flow that triggered it. The body must NEVER carry Restricted data — chat text, exact
coordinates, identity numbers — only a neutral prompt that draws the user into the app.
"""

from __future__ import annotations

import logging
import uuid

from app.integrations.push.base import PushClient
from app.repositories.device_token_repository import DeviceTokenRepository

_logger = logging.getLogger("app.services.notification")


class NotificationService:
    """Sends best-effort push notifications to users and to city couriers."""

    def __init__(self, *, devices: DeviceTokenRepository, push: PushClient) -> None:
        """Wire the device-token repository and the push client."""
        self._devices = devices
        self._push = push

    async def notify_user(self, *, user_id: uuid.UUID, title: str, body: str) -> int:
        """Push to all of a user's devices. Returns how many tokens were targeted."""
        tokens = await self._devices.tokens_for_user(user_id)
        await self._send(tokens, title, body)
        return len(tokens)

    async def notify_city_couriers(self, *, city: str, title: str, body: str) -> int:
        """Push to every active, verified courier in a city (the new-order radar ping)."""
        tokens = await self._devices.tokens_for_city_couriers(city)
        await self._send(tokens, title, body)
        return len(tokens)

    async def _send(self, tokens: list[str], title: str, body: str) -> None:
        if not tokens:
            return
        try:
            await self._push.send_push(tokens, title, body)
        except Exception:  # noqa: BLE001 - a push failure must never break the caller
            _logger.exception("push notification failed for %d token(s)", len(tokens))
