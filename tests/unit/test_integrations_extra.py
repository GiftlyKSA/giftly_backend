"""Tests for the real integration clients (mocked transport) and the client factory."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from app.core.config import Environment
from app.integrations.email.sndr_client import SndrEmailClient
from app.integrations.factory import build_clients
from app.integrations.paylink.real import RealPaylinkClient
from app.integrations.push.real import RealPushClient
from app.integrations.sms.real import RealSmsClient
from app.integrations.storage.real import S3StorageClient
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.conftest import make_test_settings


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


class _S3Context:
    def __init__(self, client: object) -> None:
        self.client = client

    async def __aenter__(self) -> object:
        return self.client

    async def __aexit__(self, *args: object) -> None:
        return None


class _S3Session:
    def __init__(self, client: object) -> None:
        self.client_instance = client

    def client(self, *args: object, **kwargs: object) -> _S3Context:
        return _S3Context(self.client_instance)


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


async def test_real_storage_signs_upload_length_and_cloudfront_read() -> None:
    client = S3StorageClient(
        bucket="private-bucket",
        region="eu-west-1",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
        cloudfront_domain="cdn.example.com",
        cloudfront_key_pair_id="K123",
        cloudfront_private_key=_private_key_pem(),
    )

    upload = await client.create_upload_url(
        storage_key="orders/pending/file.jpg",
        content_type="image/jpeg",
        byte_size=1234,
        ttl_seconds=300,
    )
    upload_query = parse_qs(urlparse(upload).query)
    assert "content-length" in upload_query["X-Amz-SignedHeaders"][0]

    read = client.signed_read_url("orders/proof/file.jpg", ttl_seconds=60)
    read_query = parse_qs(urlparse(read).query)
    assert read.startswith("https://cdn.example.com/orders/proof/file.jpg?")
    assert set(read_query) == {"Expires", "Signature", "Key-Pair-Id", "Hash-Algorithm"}
    assert read_query["Hash-Algorithm"] == ["SHA256"]


@pytest.mark.parametrize("code,missing", [("NoSuchKey", True), ("AccessDenied", False)])
async def test_real_storage_only_treats_missing_object_as_absent(code: str, missing: bool) -> None:
    class _HeadClient:
        async def head_object(self, **kwargs: object) -> object:
            raise ClientError({"Error": {"Code": code}}, "HeadObject")

    client = S3StorageClient(
        bucket="private-bucket",
        region="eu-west-1",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
        cloudfront_domain="cdn.example.com",
        cloudfront_key_pair_id="K123",
        cloudfront_private_key=_private_key_pem(),
    )
    client._session = _S3Session(_HeadClient())  # type: ignore[assignment]  # noqa: SLF001

    if missing:
        assert await client.head_object("missing.jpg") is None
    else:
        with pytest.raises(ClientError):
            await client.head_object("private.jpg")


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
        SMS_PROVIDER_KEY="sms-key",
        AWS_REGION="eu-west-1",
        AWS_ACCESS_KEY_ID="access",
        AWS_SECRET_ACCESS_KEY="secret",
        S3_BUCKET_NAME="private-bucket",
        CLOUDFRONT_DOMAIN="cdn.example.com",
        CLOUDFRONT_KEY_PAIR_ID="K123",
        CLOUDFRONT_PRIVATE_KEY=_private_key_pem(),
    )
    clients = build_clients(settings)
    assert type(clients.gateway).__name__ == "RealPaylinkClient"
    assert type(clients.email).__name__ == "SndrEmailClient"
