"""Direct payment-service and money-hold tests for the error and edge branches."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    InsufficientFundsError,
    InvalidWebhookSignatureError,
    NotFoundError,
    PaymentAmountMismatchError,
    ValidationDomainError,
)
from app.core.redis import build_redis
from app.integrations.paylink.fake import FakePaylinkClient
from app.models import Invoice, Order, User, Wallet
from app.models.enums import (
    InvoiceStatus,
    OrderStatus,
    PaymentPurpose,
    UserRole,
    WalletType,
)
from app.repositories.wallet_repository import WalletRepository
from app.services.money_service import MoneyService
from app.services.payment_service import build_payment_service
from geoalchemy2 import WKTElement
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_test_settings


def _settings() -> Settings:
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if os.environ.get("REDIS_URL"):
        overrides["REDIS_URL"] = os.environ["REDIS_URL"]
    return make_test_settings(**overrides)


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = build_redis(_settings())
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        pytest.skip(f"redis unavailable: {exc}")
    yield client
    await client.aclose()


def _service(db: AsyncSession, redis: Redis) -> object:
    return build_payment_service(
        session=db,
        gateway=FakePaylinkClient(_settings().ENVIRONMENT),
        redis=redis,
        settings=_settings(),
    )


async def _customer_with_wallet(db: AsyncSession) -> tuple[User, Wallet]:
    user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    db.add(user)
    await db.flush()
    wallet = Wallet(user_id=user.id, type=WalletType.CUSTOMER)
    db.add(wallet)
    await db.flush()
    return user, wallet


async def test_topup_rejects_out_of_bounds(db_session: AsyncSession, redis_client: Redis) -> None:
    user, _wallet = await _customer_with_wallet(db_session)
    svc = _service(db_session, redis_client)
    with pytest.raises(ValidationDomainError):
        await svc.create_topup(user_id=user.id, amount=Decimal("10.00"))  # below the min
    with pytest.raises(ValidationDomainError):
        await svc.create_topup(user_id=user.id, amount=Decimal("999999.00"))  # above the max


async def test_topup_requires_wallet(db_session: AsyncSession, redis_client: Redis) -> None:
    user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    db_session.add(user)
    await db_session.flush()  # no wallet created
    with pytest.raises(NotFoundError):
        await _service(db_session, redis_client).create_topup(
            user_id=user.id, amount=Decimal("500.00")
        )


async def _issued_invoice(db: AsyncSession) -> tuple[User, Order, Invoice]:
    user, _wallet = await _customer_with_wallet(db)
    courier = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.COURIER)
    db.add(courier)
    await db.flush()
    order = Order(
        customer_id=user.id,
        courier_id=courier.id,
        delivery_city="Jeddah",
        delivery_location=WKTElement("POINT(39.2 21.5)", srid=4326),
        delivery_date=datetime.now(UTC).date() + timedelta(days=10),
        status=OrderStatus.WAITING_PAYMENT,
    )
    db.add(order)
    await db.flush()
    invoice = Invoice(
        order_id=order.id,
        issued_by_courier_id=courier.id,
        status=InvoiceStatus.ISSUED,
        items_net_amount=Decimal("500.00"),
        courier_fee_amount=Decimal("100.00"),
        service_fee_amount=Decimal("30.00"),
        discount_amount=Decimal("0.00"),
        net_after_discount_amount=Decimal("630.00"),
        tax_amount=Decimal("94.50"),
        total_amount=Decimal("724.50"),
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=48),
    )
    db.add(invoice)
    await db.flush()
    return user, order, invoice


async def test_pay_rejects_non_issued_invoice(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    user, _order, invoice = await _issued_invoice(db_session)
    invoice.status = InvoiceStatus.PAID
    await db_session.flush()
    with pytest.raises(ConflictError):
        await _service(db_session, redis_client).pay_invoice(
            invoice_id=invoice.id, customer_id=user.id
        )


async def test_pay_rejects_expired_invoice(db_session: AsyncSession, redis_client: Redis) -> None:
    user, _order, invoice = await _issued_invoice(db_session)
    invoice.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()
    with pytest.raises(ConflictError):
        await _service(db_session, redis_client).pay_invoice(
            invoice_id=invoice.id, customer_id=user.id
        )


async def test_pay_unknown_invoice_is_404(db_session: AsyncSession, redis_client: Redis) -> None:
    user, _wallet = await _customer_with_wallet(db_session)
    with pytest.raises(NotFoundError):
        await _service(db_session, redis_client).pay_invoice(
            invoice_id=uuid.uuid4(), customer_id=user.id
        )


async def test_hold_funds_rejects_insufficient(db_session: AsyncSession) -> None:
    _user, wallet = await _customer_with_wallet(db_session)
    money = MoneyService(WalletRepository(db_session))
    with pytest.raises(InsufficientFundsError):
        await money.hold_funds(wallet_id=wallet.id, amount=Decimal("50.00"))


async def test_webhook_rejects_bad_signature(db_session: AsyncSession, redis_client: Redis) -> None:
    with pytest.raises(InvalidWebhookSignatureError):
        await _service(db_session, redis_client).handle_webhook(
            raw_body=b'{"transaction_no":"x","status":"PAID","amount":"1.00"}',
            signature="bad",
        )


async def test_webhook_unknown_transaction_is_404(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    svc = _service(db_session, redis_client)
    gateway = FakePaylinkClient(_settings().ENVIRONMENT)
    body = b'{"transaction_no":"UNKNOWN-TXN","status":"PAID","amount":"1.00"}'
    with pytest.raises(NotFoundError):
        # Sign with the same test secret the service's gateway verifies against.
        await svc.handle_webhook(raw_body=body, signature=gateway.sign(body))


async def test_webhook_amount_mismatch_rejected(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    from app.repositories.payment_repository import PaymentRepository

    user, _wallet = await _customer_with_wallet(db_session)
    payments = PaymentRepository(db_session)
    intent = await payments.create_intent(
        user_id=user.id,
        purpose=PaymentPurpose.WALLET_TOPUP,
        amount=Decimal("500.00"),
        reference_invoice_id=None,
        expires_at=datetime.now(UTC) + timedelta(hours=48),
    )
    await payments.attach_gateway(intent, transaction_no="TXN-MISMATCH", url="http://x")

    gateway = FakePaylinkClient(_settings().ENVIRONMENT)
    body = b'{"transaction_no":"TXN-MISMATCH","status":"PAID","amount":"999.00"}'
    with pytest.raises(PaymentAmountMismatchError):
        await _service(db_session, redis_client).handle_webhook(
            raw_body=body, signature=gateway.sign(body)
        )


def _signed(body: bytes) -> str:
    return FakePaylinkClient(_settings().ENVIRONMENT).sign(body)


def _body(txn: str, amount: str, status: str = "PAID") -> bytes:
    import json as _json

    return _json.dumps({"transaction_no": txn, "status": status, "amount": amount}).encode("utf-8")


async def test_create_topup_and_settle_via_webhook(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    user, wallet = await _customer_with_wallet(db_session)
    svc = _service(db_session, redis_client)
    result = await svc.create_topup(user_id=user.id, amount=Decimal("500.00"))
    assert result.payment_url and result.amount == Decimal("500.00")

    intent = await svc._payments.get_intent(result.intent_id)  # type: ignore[attr-defined]
    body = _body(intent.paylink_transaction_no, "500.00")
    out = await svc.handle_webhook(raw_body=body, signature=_signed(body))
    assert out.outcome == "processed"
    await db_session.refresh(wallet)
    assert wallet.balance == Decimal("500.00")


async def test_pay_invoice_from_wallet_settles(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    user, order, invoice = await _issued_invoice(db_session)
    wallet = await WalletRepository(db_session).get_by_user(user.id)
    assert wallet is not None
    wallet.balance = Decimal("1000.00")  # fund the wallet directly for this branch test
    await db_session.flush()

    svc = _service(db_session, redis_client)
    result = await svc.pay_invoice(invoice_id=invoice.id, customer_id=user.id)
    assert result.status == "PAID"
    assert result.amount_from_wallet == Decimal("724.50")
    assert invoice.status is InvoiceStatus.PAID
    assert order.status is OrderStatus.IN_PROGRESS
    await db_session.refresh(wallet)
    assert wallet.balance == Decimal("275.50")


async def test_pay_invoice_via_gateway_then_webhook_settles(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    from app.repositories.payment_repository import PaymentRepository

    user, order, invoice = await _issued_invoice(db_session)  # wallet balance 0
    svc = _service(db_session, redis_client)
    result = await svc.pay_invoice(invoice_id=invoice.id, customer_id=user.id)
    assert result.status == "PENDING"
    assert result.amount_from_gateway == Decimal("724.50")

    intent = await PaymentRepository(db_session).get_open_intent_for_invoice(invoice.id)
    assert intent is not None
    body = _body(intent.paylink_transaction_no, "724.50")
    out = await svc.handle_webhook(raw_body=body, signature=_signed(body))
    assert out.outcome == "processed"
    assert invoice.status is InvoiceStatus.PAID
    assert order.status is OrderStatus.IN_PROGRESS


async def test_webhook_marks_intent_failed_on_non_paid(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    from app.repositories.payment_repository import PaymentRepository

    user, _wallet = await _customer_with_wallet(db_session)
    payments = PaymentRepository(db_session)
    intent = await payments.create_intent(
        user_id=user.id,
        purpose=PaymentPurpose.WALLET_TOPUP,
        amount=Decimal("500.00"),
        reference_invoice_id=None,
        expires_at=datetime.now(UTC) + timedelta(hours=48),
    )
    await payments.attach_gateway(intent, transaction_no="TXN-FAIL", url="http://x")
    body = b'{"transaction_no":"TXN-FAIL","status":"FAILED","amount":"500.00"}'
    out = await _service(db_session, redis_client).handle_webhook(
        raw_body=body, signature=_signed(body)
    )
    assert out.outcome == "failed"
