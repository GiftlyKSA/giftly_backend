"""The push-notification client contract (SPEC SECTION 5.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class PushClient(ABC):
    """Sends push notifications to a user's devices."""

    @abstractmethod
    async def send_push(self, tokens: list[str], title: str, body: str) -> None:
        """Send a push to ``tokens``.

        Note:
            The body must never contain Restricted data (e.g. chat message text).
        """
