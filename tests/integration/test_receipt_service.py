"""Receipt-service and sweeper tests: exactly one receipt on PAID, idempotent, no-email.

The system sends exactly ONE email — the invoice-paid receipt. These assert it fires once
on a PAID invoice, never twice, and never for an unpaid one, using the in-memory
FakeEmailClient so nothing touches the network.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Environment, Settings
from app.integrations.email.fake import FakeEmailClient
from app.models import Invoice, Order, User
from app.models.enums import InvoiceStatus, OrderStatus, UserRole
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.user_repository import UserRepository
from app.services.receipt_service import ReceiptService
from geoalchemy2 import WKTElement
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_test_settings


def _settings() -> Settings:
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    return make_test_settings(**overrides)


def _service(db: AsyncSession, email: FakeEmailClient) -> ReceiptService:
    return ReceiptService(
        invoices=InvoiceRepository(db),
        orders=OrderRepository(db),
        users=UserRepository(db),
        email=email,
        settings=_settings(),
    )


async def _paid_invoice(db: AsyncSession, *, email: str | None) -> Invoice:
    customer = User(
        phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}",
        role=UserRole.CUSTOMER,
        email=email,
    )
    courier = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.COURIER)
    db.add_all([customer, courier])
    await db.flush()
    order = Order(
        customer_id=customer.id,
        courier_id=courier.id,
        delivery_city="Jeddah",
        delivery_location=WKTElement("POINT(39.2 21.5)", srid=4326),
        delivery_date=datetime.now(UTC).date() + timedelta(days=10),
        status=OrderStatus.IN_PROGRESS,
    )
    db.add(order)
    await db.flush()
    invoice = Invoice(
        order_id=order.id,
        issued_by_courier_id=courier.id,
        status=InvoiceStatus.PAID,
        items_net_amount=Decimal("500.00"),
        courier_fee_amount=Decimal("100.00"),
        service_fee_amount=Decimal("30.00"),
        discount_amount=Decimal("0.00"),
        net_after_discount_amount=Decimal("630.00"),
        tax_amount=Decimal("94.50"),
        total_amount=Decimal("724.50"),
        issued_at=datetime.now(UTC),
        paid_at=datetime.now(UTC),
    )
    db.add(invoice)
    await db.flush()
    return invoice


async def test_receipt_sent_once_on_paid(db_session: AsyncSession) -> None:
    email = FakeEmailClient(Environment.TEST)
    invoice = await _paid_invoice(db_session, email="buyer@example.com")

    assert await _service(db_session, email).send_receipt(invoice.id) is True
    assert len(email.sent) == 1
    sent = email.sent[0]
    assert sent.to_email == "buyer@example.com"
    assert sent.variables["total_amount"] == "724.50"
    assert sent.variables["order_id"] == str(invoice.order_id)
    # No Restricted data leaks into the template variables.
    assert "phone" not in sent.variables and "latitude" not in sent.variables
    assert invoice.receipt_email_sent_at is not None

    # A second attempt is a no-op — never two receipts.
    assert await _service(db_session, email).send_receipt(invoice.id) is False
    assert len(email.sent) == 1


async def test_receipt_skipped_when_no_email(db_session: AsyncSession) -> None:
    email = FakeEmailClient(Environment.TEST)
    invoice = await _paid_invoice(db_session, email=None)
    assert await _service(db_session, email).send_receipt(invoice.id) is False
    assert email.sent == []
    # Still stamped so the sweeper stops retrying it.
    assert invoice.receipt_email_sent_at is not None


async def test_receipt_not_sent_for_unpaid_invoice(db_session: AsyncSession) -> None:
    email = FakeEmailClient(Environment.TEST)
    invoice = await _paid_invoice(db_session, email="buyer@example.com")
    invoice.status = InvoiceStatus.ISSUED
    invoice.receipt_email_sent_at = None
    await db_session.flush()
    assert await _service(db_session, email).send_receipt(invoice.id) is False
    assert email.sent == []


async def test_receipt_missing_invoice_is_noop(db_session: AsyncSession) -> None:
    email = FakeEmailClient(Environment.TEST)
    assert await _service(db_session, email).send_receipt(uuid.uuid4()) is False
    assert email.sent == []
