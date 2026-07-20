"""Real push client via Supabase webhook / FCM / APNs (SPEC SECTION 5.1)."""

from __future__ import annotations

import httpx

from app.integrations.push.base import PushClient


class RealPushClient(PushClient):
    """Dispatches pushes through the configured Supabase/FCM edge function."""

    def __init__(self, supabase_url: str, service_key: str, timeout_seconds: float = 10.0) -> None:
        """Hold the push dispatch credentials and one pooled HTTP client (audit PERF-1)."""
        self._url = supabase_url.rstrip("/")
        self._service_key = service_key
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        """Close the pooled HTTP client (wired to app shutdown)."""
        await self._client.aclose()

    async def send_push(self, tokens: list[str], title: str, body: str) -> None:
        """Send the push; the body never contains Restricted data."""
        # VENDOR CONTRACT — refine against the Supabase Edge Function contract.
        response = await self._client.post(
            f"{self._url}/functions/v1/push",
            json={"tokens": tokens, "title": title, "body": body},
            headers={"Authorization": f"Bearer {self._service_key}"},
        )
        response.raise_for_status()
