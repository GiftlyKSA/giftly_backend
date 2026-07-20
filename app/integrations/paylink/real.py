"""Real Paylink gateway client (SPEC SECTION 5.1, 17.2 A08).

The webhook signature is verified over the RAW body with ``compare_digest``; never
re-serialize the payload before verifying, as that changes bytes and breaks the
signature. A synchronous vendor SDK call, if introduced, MUST be wrapped in
``run_in_threadpool`` — one blocking call stalls the whole event loop.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal

import httpx

from app.integrations.paylink.base import GatewayCharge, PaymentGateway


class RealPaylinkClient(PaymentGateway):
    """Talks to the live Paylink API over HTTPS."""

    def __init__(
        self,
        *,
        api_id: str,
        secret_key: str,
        webhook_secret: str,
        base_url: str = "https://restapi.paylink.sa",
        timeout_seconds: float = 15.0,
    ) -> None:
        """Hold live credentials, the webhook secret, and one pooled HTTP client."""
        self._api_id = api_id
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret.encode("utf-8")
        self._base_url = base_url.rstrip("/")
        # One long-lived client (audit PERF-1): connection pooling and TLS reuse
        # instead of a fresh handshake on every charge — this is the payment path.
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        """Close the pooled HTTP client (wired to app shutdown)."""
        await self._client.aclose()

    async def create_charge(
        self, *, amount: Decimal, order_number: str, callback_url: str
    ) -> GatewayCharge:
        """Create a live charge and return its transaction number and URL."""
        # VENDOR CONTRACT — refine field names against Paylink's live API docs.
        payload = {
            "amount": str(amount),
            "orderNumber": order_number,
            "callBackUrl": callback_url,
            "apiId": self._api_id,
            "secretKey": self._secret_key,
        }
        response = await self._client.post(f"{self._base_url}/api/addInvoice", json=payload)
        response.raise_for_status()
        data = response.json()
        return GatewayCharge(
            transaction_no=str(data["transactionNo"]),
            payment_url=str(data["url"]),
        )

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verify the HMAC-SHA256 over the raw body in constant time."""
        expected = hmac.new(self._webhook_secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
