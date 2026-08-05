"""Contract tests for the StreamPay payment-link client."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest
from app.core.config import Environment
from app.integrations.streampay.base import StreamPayCustomer, StreamPayItem
from app.integrations.streampay.fake import FakeStreamPayClient
from app.integrations.streampay.real import RealStreamPayClient


def _mock_httpx(
    monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    original = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return requests


async def test_real_streampay_creates_consumer_products_and_hosted_payment_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _mock_httpx(
        monkeypatch,
        [
            httpx.Response(201, json={"id": "consumer-1"}),
            httpx.Response(201, json={"id": "product-1"}),
            httpx.Response(201, json={"id": "product-2"}),
            httpx.Response(201, json={"id": "link-1", "url": "https://checkout.streampay.sa/l/1"}),
        ],
    )
    client = RealStreamPayClient(api_key="key", api_secret="secret", webhook_secret="webhook")

    checkout = await client.create_payment_link(
        reference="intent-1",
        customer=StreamPayCustomer(
            external_id="customer-1",
            name="Ada Lovelace",
            phone_number="+966501234567",
            email="ada@example.com",
        ),
        items=(
            StreamPayItem(name="Gift wrap", description="Premium wrap", amount=Decimal("30.00")),
            StreamPayItem(name="Delivery", description=None, amount=Decimal("20.50")),
        ),
        success_redirect_url="https://app.example.com/payment/success",
        failure_redirect_url="https://app.example.com/payment/failure",
    )

    assert checkout.payment_link_id == "link-1"
    assert checkout.payment_url == "https://checkout.streampay.sa/l/1"
    assert requests[0].url.path == "/api/v2/consumers"
    assert json.loads(requests[0].content) == {
        "external_id": "customer-1",
        "name": "Ada Lovelace",
        "phone_number": "+966501234567",
        "email": "ada@example.com",
    }
    assert requests[0].headers["x-api-key"] == base64.b64encode(b"key:secret").decode()
    assert json.loads(requests[1].content)["prices"] == [
        {
            "amount": "30.00",
            "currency": "SAR",
            "is_price_inclusive_of_vat": True,
            "is_price_exempt_from_vat": False,
        }
    ]
    assert requests[3].url.path == "/api/v2/payment_links"
    assert json.loads(requests[3].content)["items"] == [
        {"product_id": "product-1", "quantity": 1},
        {"product_id": "product-2", "quantity": 1},
    ]
    assert json.loads(requests[3].content)["organization_consumer_id"] == "consumer-1"
    assert json.loads(requests[3].content)["custom_metadata"] == {"payment_intent_id": "intent-1"}


def test_real_streampay_verifies_timestamped_webhook_signature() -> None:
    client = RealStreamPayClient(api_key="key", api_secret="secret", webhook_secret="webhook")
    body = b'{"event_type":"PAYMENT_SUCCEEDED"}'
    timestamp = "1720000000"
    digest = hmac.new(b"webhook", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()

    assert client.verify_webhook_signature(body, f"t={timestamp},v1={digest}")
    assert not client.verify_webhook_signature(body, f"t={timestamp},v1=bad")


async def test_fake_streampay_returns_a_local_payment_link_and_signed_webhook() -> None:
    client = FakeStreamPayClient(Environment.TEST)
    checkout = await client.create_payment_link(
        reference="intent-1",
        customer=StreamPayCustomer("customer-1", "Ada", "+966501234567", None),
        items=(StreamPayItem("Wallet top-up", None, Decimal("100.00")),),
        success_redirect_url=None,
        failure_redirect_url=None,
    )
    body = b'{"event_type":"PAYMENT_SUCCEEDED"}'

    assert checkout.payment_link_id.startswith("FAKE-STREAM-LINK-")
    assert "payment_link_id=" + checkout.payment_link_id in checkout.payment_url
    assert client.verify_webhook_signature(body, client.sign(body, timestamp="1720000000"))
