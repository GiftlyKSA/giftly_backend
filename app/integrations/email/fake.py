"""In-memory email double for development and test (SPEC SECTION 5.3 / 23)."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Environment
from app.integrations._guard import forbid_in_production
from app.integrations.email.base import EmailClient


@dataclass
class RecordedEmail:
    """One captured send, for test assertions."""

    to_email: str
    template_key: str
    variables: dict[str, object]


class FakeEmailClient(EmailClient):
    """Records every send in memory; never touches the network.

    Tests assert exactly one send on invoice PAID and zero on every other event.
    """

    def __init__(self, environment: Environment) -> None:
        """Refuse construction in production, then start with an empty log."""
        forbid_in_production(environment, type(self).__name__)
        self.sent: list[RecordedEmail] = []

    async def send_transactional(
        self, to_email: str, template_key: str, variables: dict[str, object]
    ) -> None:
        """Record the send instead of dispatching it."""
        self.sent.append(RecordedEmail(to_email, template_key, dict(variables)))
