"""Live StreamPay payment-link client."""

from __future__ import annotations

import base64
import hashlib
import hmac
from decimal import Decimal
from typing import Any

import httpx

from app.integrations.streampay.base import (
    StreamPayCheckout,
    StreamPayClient,
    StreamPayCustomer,
    StreamPayItem,
)


class RealStreamPayClient(StreamPayClient):
    """Create StreamPay consumers, one-time products, and hosted payment links."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        webhook_secret: str,
        base_url: str = "https://stream-app-service.streampay.sa/api/v2",
        timeout_seconds: float = 15.0,
    ) -> None:
        """Configure one pooled, authenticated HTTP client for StreamPay."""
        credential = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode("ascii")
        self._webhook_secret = webhook_secret.encode("utf-8")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"x-api-key": credential, "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        """Release the pooled HTTP connections on application shutdown."""
        await self._client.aclose()

    async def create_payment_link(
        self,
        *,
        reference: str,
        customer: StreamPayCustomer,
        items: tuple[StreamPayItem, ...],
        success_redirect_url: str | None,
        failure_redirect_url: str | None,
    ) -> StreamPayCheckout:
        """Create a consumer, one-time products, and a single-use hosted checkout."""
        consumer_id = await self._create_consumer(customer)
        product_ids = [await self._create_product(item, reference) for item in items]
        payload: dict[str, Any] = {
            "name": f"SAFE-GIFT invoice {reference}",
            "currency": "SAR",
            "items": [
                {"product_id": product_id, "quantity": 1} for product_id in product_ids
            ],
            "max_number_of_payments": 1,
            "organization_consumer_id": consumer_id,
            "contact_information_type": "PHONE",
            "custom_metadata": {"payment_intent_id": reference},
        }
        if success_redirect_url:
            payload["success_redirect_url"] = success_redirect_url
        if failure_redirect_url:
            payload["failure_redirect_url"] = failure_redirect_url
        data = await self._post("/payment_links", payload)
        return StreamPayCheckout(
            payment_link_id=self._required_value(data, "id"),
            payment_url=self._required_value(data, "url"),
        )

    async def _create_consumer(self, customer: StreamPayCustomer) -> str:
        payload: dict[str, str] = {
            "external_id": customer.external_id,
            "name": customer.name,
            "phone_number": customer.phone_number,
        }
        if customer.email:
            payload["email"] = customer.email
        data = await self._post("/consumers", payload)
        return self._required_value(data, "id")

    async def _create_product(self, item: StreamPayItem, reference: str) -> str:
        payload: dict[str, Any] = {
            "name": item.name[:160],
            "type": "ONE_OFF",
            "is_one_time": True,
            "prices": [
                {
                    "amount": self._money(item.amount),
                    "currency": "SAR",
                    "is_price_inclusive_of_vat": True,
                    "is_price_exempt_from_vat": False,
                }
            ],
            "external_metadata": {"payment_intent_id": reference},
        }
        if item.description:
            payload["description"] = item.description[:512]
        data = await self._post("/products", payload)
        return self._required_value(data, "id")

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(f"{self._base_url}{path}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected StreamPay response.")
        return data

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verify `t=<unix>,v1=<digest>` over `timestamp.raw-body` in constant time."""
        timestamp, provided = self._signature_parts(signature)
        if timestamp is None or provided is None:
            return False
        message = timestamp.encode("utf-8") + b"." + raw_body
        expected = hmac.new(self._webhook_secret, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided)

    @staticmethod
    def _signature_parts(signature: str) -> tuple[str | None, str | None]:
        values: dict[str, str] = {}
        for part in signature.split(","):
            key, separator, value = part.strip().partition("=")
            if separator and key and value:
                values[key] = value
        return values.get("t"), values.get("v1")

    @staticmethod
    def _required_value(data: dict[str, Any], key: str) -> str:
        value = data.get(key)
        if value is None or not str(value):
            raise ValueError(f"StreamPay response did not include {key}.")
        return str(value)

    @staticmethod
    def _money(amount: Decimal) -> str:
        return f"{amount:.2f}"
