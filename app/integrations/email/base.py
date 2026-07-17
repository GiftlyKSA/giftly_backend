"""The email client contract (SPEC SECTION 5.3).

Services depend on this ABC only; nothing outside ``sndr_client.py`` knows the
vendor wire format. The system sends exactly ONE email: the invoice-paid receipt.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class EmailClient(ABC):
    """Sends transactional email via templates keyed by the provider."""

    @abstractmethod
    async def send_transactional(
        self, to_email: str, template_key: str, variables: dict[str, object]
    ) -> None:
        """Send one transactional template render.

        Args:
            to_email: Recipient address.
            template_key: Provider template identifier.
            variables: Template substitution variables (never Restricted data).
        """
