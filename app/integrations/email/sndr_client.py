"""sndr.sh email client — the ONLY place that knows the vendor wire format.

The official sndr.sh docs are not yet available. The request/response mapping below
is isolated so it can be corrected in exactly one place when the docs arrive; see
the VENDOR CONTRACT block. Services never import a sndr symbol.
"""

from __future__ import annotations

import httpx

from app.integrations.email.base import EmailClient


class SndrEmailClient(EmailClient):
    """Sends transactional email through sndr.sh over HTTPS via httpx."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        from_email: str,
        from_name: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Hold the vendor configuration for later sends."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._from_email = from_email
        self._from_name = from_name
        self._timeout = timeout_seconds

    async def send_transactional(
        self, to_email: str, template_key: str, variables: dict[str, object]
    ) -> None:
        """POST a template send to sndr.sh, raising on a non-2xx response."""
        # VENDOR CONTRACT — pending official sndr.sh docs. Correct this block only.
        payload = {
            "from": {"email": self._from_email, "name": self._from_name},
            "to": [{"email": to_email}],
            "template": template_key,
            "variables": variables,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/v1/transactional", json=payload, headers=headers
            )
            response.raise_for_status()
