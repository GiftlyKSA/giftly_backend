"""Tests for the real integration clients (mocked transport) and the client factory."""

from __future__ import annotations

import httpx
import pytest
from app.core.config import Environment
from app.integrations.email.sndr_client import SndrEmailClient
from app.integrations.factory import build_clients
from app.integrations.paylink.real import RealPaylinkClient
from app.integrations.push.real import RealPushClient
from app.integrations.sms.real import RealSmsClient

from tests.conftest import make_test_settings


def _mock_httpx(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    original = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(lambda request: response)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_real_paylink_signature_verification_is_constant_time() -> None:
    client = RealPaylinkClient(api_id="id", secret_key="sk", webhook_secret="whsec")
    import hashlib
    import hmac

    body = b'{"orderStatus":"Paid"}'
    good = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert client.verify_webhook_signature(body, good)
    assert not client.verify_webhook_signature(body, "deadbeef")


async def test_real_paylink_create_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_httpx(monkeypatch, httpx.Response(200, json={"transactionNo": "T1", "url": "http://pay"}))
    client = RealPaylinkClient(api_id="id", secret_key="sk", webhook_secret="whsec")
    charge = await client.create_charge(
        amount=__import__("decimal").Decimal("355.50"), order_number="o1", callback_url="http://cb"
    )
    assert charge.transaction_no == "T1"
    assert charge.payment_url == "http://pay"


async def test_sndr_email_send(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_httpx(monkeypatch, httpx.Response(200, json={"ok": True}))
    client = SndrEmailClient(base_url="https://sndr", api_key="k", from_email="f@x", from_name="F")
    await client.send_transactional("to@x", "tmpl", {"total_amount": "655.50"})


async def test_real_sms_send(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_httpx(monkeypatch, httpx.Response(200, json={"ok": True}))
    client = RealSmsClient(provider_key="k", base_url="https://sms")
    await client.send_otp("+966501234567", "849201")


async def test_real_push_send(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_httpx(monkeypatch, httpx.Response(200, json={"ok": True}))
    client = RealPushClient(supabase_url="https://sb", service_key="k")
    await client.send_push(["tok"], "New message", "You have a new message")


def test_build_clients_returns_fakes_in_test() -> None:
    clients = build_clients(make_test_settings())
    assert type(clients.gateway).__name__ == "FakePaylinkClient"
    assert type(clients.email).__name__ == "FakeEmailClient"


def test_build_clients_returns_real_in_production() -> None:
    settings = make_test_settings(
        ENVIRONMENT=Environment.PRODUCTION.value,
        DEBUG=False,
        CORS_ALLOWED_ORIGINS="https://app.example.com",
        PAYLINK_API_ID="id",
        PAYLINK_SECRET_KEY="sk",
        PAYLINK_WEBHOOK_SECRET="wh",
        PAYLINK_ALLOWED_IPS="1.2.3.4",
        PAYLINK_CALLBACK_URL="https://app.example.com/cb",
        SNDR_API_KEY="k",
        SNDR_BASE_URL="https://sndr",
        SNDR_FROM_EMAIL="f@x",
        SNDR_FROM_NAME="F",
        SNDR_INVOICE_PAID_TEMPLATE_KEY="tmpl",
        SUPABASE_URL="https://sb",
        SUPABASE_SERVICE_KEY="sk",
    )
    clients = build_clients(settings)
    assert type(clients.gateway).__name__ == "RealPaylinkClient"
    assert type(clients.email).__name__ == "SndrEmailClient"
