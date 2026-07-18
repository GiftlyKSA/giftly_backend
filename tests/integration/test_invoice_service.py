"""Direct invoice-service tests for the validation, state, and promo-edge branches."""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationDomainError,
)
from app.models import Order, Promo, User
from app.models.enums import InvoiceStatus, OrderStatus, PromoDiscountType, UserRole
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.promo_repository import PromoRepository
from app.services.invoice_service import (
    InvoiceLineInput,
    InvoiceService,
    NewInvoiceInput,
)
from app.services.promo_service import PromoService
from geoalchemy2 import WKTElement
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_test_settings


def _settings() -> Settings:
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    return make_test_settings(**overrides)


def _service(db: AsyncSession) -> InvoiceService:
    return InvoiceService(
        invoices=InvoiceRepository(db),
        orders=OrderRepository(db),
        promos=PromoService(PromoRepository(db)),
        settings=_settings(),
    )


async def _user(db: AsyncSession, role: UserRole) -> User:
    user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=role)
    db.add(user)
    await db.flush()
    return user


async def _assigned_order(db: AsyncSession) -> Order:
    customer = await _user(db, UserRole.CUSTOMER)
    courier = await _user(db, UserRole.COURIER)
    order = Order(
        customer_id=customer.id,
        courier_id=courier.id,
        delivery_city="Jeddah",
        delivery_location=WKTElement("POINT(39.2 21.5)", srid=4326),
        delivery_date=date.today() + timedelta(days=20),
        status=OrderStatus.ASSIGNED,
    )
    db.add(order)
    await db.flush()
    return order


def _line(**over: object) -> InvoiceLineInput:
    base: dict[str, object] = {
        "title": "Vase",
        "unit_price_amount": Decimal("400.00"),
        "quantity": 1,
        "tax_rate": Decimal("0.15"),
        "description": None,
    }
    base.update(over)
    return InvoiceLineInput(**base)  # type: ignore[arg-type]


def _input(**over: object) -> NewInvoiceInput:
    base: dict[str, object] = {
        "items": [_line()],
        "courier_fee_amount": Decimal("0.00"),
        "promo_code": None,
    }
    base.update(over)
    return NewInvoiceInput(**base)  # type: ignore[arg-type]


async def test_create_rejects_empty_items(db_session: AsyncSession) -> None:
    order = await _assigned_order(db_session)
    with pytest.raises(ValidationDomainError):
        await _service(db_session).create_invoice(
            order_id=order.id, courier_id=order.courier_id, data=_input(items=[])
        )


async def test_create_rejects_non_new_order_state(db_session: AsyncSession) -> None:
    order = await _assigned_order(db_session)
    order.status = OrderStatus.IN_PROGRESS
    await db_session.flush()
    with pytest.raises(InvalidStateTransitionError):
        await _service(db_session).create_invoice(
            order_id=order.id, courier_id=order.courier_id, data=_input()
        )


async def test_create_rejects_second_active_invoice(db_session: AsyncSession) -> None:
    order = await _assigned_order(db_session)
    svc = _service(db_session)
    await svc.create_invoice(order_id=order.id, courier_id=order.courier_id, data=_input())
    # The order is now WAITING_PAYMENT with an active invoice; a second one conflicts.
    order.status = OrderStatus.ASSIGNED  # force the state past the guard to hit the check
    await db_session.flush()
    with pytest.raises(ConflictError):
        await svc.create_invoice(order_id=order.id, courier_id=order.courier_id, data=_input())


async def test_create_rejects_promo_with_zero_discount(db_session: AsyncSession) -> None:
    order = await _assigned_order(db_session)
    # A fixed 0.01 promo on a tiny base still discounts; instead use a percent promo whose
    # rounded discount is 0 by pairing it with a 1-halala line.
    promo = Promo(
        code=f"Z{uuid.uuid4().hex[:6].upper()}",
        description="degenerate",
        discount_type=PromoDiscountType.PERCENT,
        percent_value=Decimal("1.00"),
        max_discount_amount=Decimal("0.00"),  # cap forces the discount to 0.00
        min_order_amount=Decimal("0.00"),
        max_usages_per_user=5,
    )
    db_session.add(promo)
    await db_session.flush()
    with pytest.raises(ValidationDomainError):
        await _service(db_session).create_invoice(
            order_id=order.id, courier_id=order.courier_id, data=_input(promo_code=promo.code)
        )


async def test_cancel_rejects_unknown_invoice(db_session: AsyncSession) -> None:
    courier = await _user(db_session, UserRole.COURIER)
    with pytest.raises(NotFoundError):
        await _service(db_session).cancel_invoice(invoice_id=uuid.uuid4(), courier_id=courier.id)


async def test_preview_requires_active_invoice(db_session: AsyncSession) -> None:
    order = await _assigned_order(db_session)
    with pytest.raises(NotFoundError):
        await _service(db_session).preview_promo(
            order_id=order.id, code="WELCOME10", customer_id=order.customer_id
        )


async def test_create_issue_reads_and_cancel_reopen(db_session: AsyncSession) -> None:
    order = await _assigned_order(db_session)
    svc = _service(db_session)
    invoice = await svc.create_invoice(
        order_id=order.id, courier_id=order.courier_id, data=_input()
    )
    assert invoice.status is InvoiceStatus.ISSUED
    assert order.status is OrderStatus.WAITING_PAYMENT

    # Participants can read the invoice and its lines.
    got, items = await svc.get_invoice_for_actor(invoice_id=invoice.id, actor_id=order.customer_id)
    assert got.id == invoice.id and len(items) == 1
    by_order, _ = await svc.get_active_invoice_for_order(
        order_id=order.id, actor_id=order.courier_id
    )
    assert by_order.id == invoice.id

    # Cancelling reopens the order for re-authoring.
    cancelled = await svc.cancel_invoice(invoice_id=invoice.id, courier_id=order.courier_id)
    assert cancelled.status is InvoiceStatus.CANCELLED
    assert order.status is OrderStatus.ASSIGNED
    assert order.total_amount == Decimal("0.00")
    # An already-cancelled invoice cannot be cancelled again.
    with pytest.raises(InvalidStateTransitionError):
        await svc.cancel_invoice(invoice_id=invoice.id, courier_id=order.courier_id)


async def test_preview_promo_matches_golden(db_session: AsyncSession) -> None:
    order = await _assigned_order(db_session)
    svc = _service(db_session)
    # Issue the golden invoice with no promo (items 500 + courier 100).
    await svc.create_invoice(
        order_id=order.id,
        courier_id=order.courier_id,
        data=_input(
            items=[
                _line(unit_price_amount=Decimal("400.00"), quantity=1),
                _line(title="Wrapping", unit_price_amount=Decimal("50.00"), quantity=2),
            ],
            courier_fee_amount=Decimal("100.00"),
        ),
    )
    promo = Promo(
        code=f"WEL{uuid.uuid4().hex[:6].upper()}",
        description="ten percent",
        discount_type=PromoDiscountType.PERCENT,
        percent_value=Decimal("10.00"),
        max_discount_amount=Decimal("100.00"),
        min_order_amount=Decimal("0.00"),
        max_usages_per_user=5,
    )
    db_session.add(promo)
    await db_session.flush()
    preview = await svc.preview_promo(
        order_id=order.id, code=promo.code, customer_id=order.customer_id
    )
    assert preview.discount_amount == Decimal("60.00")
    assert preview.original_total_amount == Decimal("724.50")
    assert preview.total_amount == Decimal("655.50")


@pytest.mark.parametrize(
    "line",
    [
        _line(unit_price_amount=Decimal("0.00")),
        _line(unit_price_amount=Decimal("999999.00")),
        _line(quantity=1000),
        _line(tax_rate=Decimal("2.00")),
    ],
)
async def test_create_rejects_out_of_bounds_line(
    db_session: AsyncSession, line: InvoiceLineInput
) -> None:
    order = await _assigned_order(db_session)
    with pytest.raises(ValidationDomainError):
        await _service(db_session).create_invoice(
            order_id=order.id, courier_id=order.courier_id, data=_input(items=[line])
        )


async def test_create_rejects_too_many_items(db_session: AsyncSession) -> None:
    order = await _assigned_order(db_session)
    with pytest.raises(ValidationDomainError):
        await _service(db_session).create_invoice(
            order_id=order.id,
            courier_id=order.courier_id,
            data=_input(items=[_line() for _ in range(21)]),
        )
