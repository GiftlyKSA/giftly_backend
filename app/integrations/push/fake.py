"""In-memory push double (SPEC SECTION 5.1 / 23)."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Environment
from app.integrations._guard import forbid_in_production
from app.integrations.push.base import PushClient


@dataclass
class RecordedPush:
    """One captured push, for test assertions."""

    tokens: list[str]
    title: str
    body: str


class FakePushClient(PushClient):
    """Records every push in memory; never touches FCM/APNs."""

    def __init__(self, environment: Environment) -> None:
        """Refuse construction in production, then start empty."""
        forbid_in_production(environment, type(self).__name__)
        self.sent: list[RecordedPush] = []

    async def send_push(self, tokens: list[str], title: str, body: str) -> None:
        """Record the push instead of dispatching it."""
        self.sent.append(RecordedPush(list(tokens), title, body))
