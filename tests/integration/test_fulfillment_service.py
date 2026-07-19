"""Fulfilment-service tests: geofenced delivery, escrow release, disputes, auto-approve.

The escrow lifecycle is money-critical: every test that moves money re-runs the ledger
reconciliation to prove no drift. Funds are moved only through the double-entry ledger.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.config import Environment, Settings
from app.core.exceptions import ConflictError, InvalidStateTransitionError, ValidationDomainError
from app.integrations.storage.fake import FakeStorageClient
from app.models import Invoice, Order, User, Wallet
from app.models.enums import (
    DisputeStatus,
    InvoiceStatus,
    OrderStatus,
    TransactionType,
    UserRole,
    WalletType,
)
from app.repositories.dispute_repository import DisputeRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.wallet_repository import WalletRepository
from app.services.fulfillment_service import DeliveryInput, FulfillmentService
from app.services.media_service import MediaService
from app.services.money_service import Leg, MoneyService
from geoalchemy2 import WKTElement
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_test_settings

_LNG, _LAT = 39.2000, 21.5000


def _settings() -> Settings:
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    return make_test_settings(**overrides)


def _service(db: AsyncSession) -> FulfillmentService:
    settings = _settings()
    return FulfillmentService(
        orders=OrderRepository(db),
        invoices=InvoiceRepository(db),
        disputes=DisputeRepository(db),
        wallets=WalletRepository(db),
        money=MoneyService(WalletRepository(db)),
        media=MediaService(FakeStorageClient(Environment.TEST), settings),
        settings=settings,
    )


async def _user_wallet(db: AsyncSession, role: UserRole, wtype: WalletType) -> tuple[User, Wallet]:
    user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=role)
    db.add(user)
    await db.flush()
    wallet = Wallet(user_id=user.id, type=wtype)
    db.add(wallet)
    await db.flush()
    return user, wallet


async def _paid_order(
    db: AsyncSession, *, status: OrderStatus = OrderStatus.IN_PROGRESS
) -> tuple[User, User, Order, Invoice]:
    """A funded, paid order: escrow holds the 724.50 total, ready to release."""
    customer, _cw = await _user_wallet(db, UserRole.CUSTOMER, WalletType.CUSTOMER)
    courier, _kw = await _user_wallet(db, UserRole.COURIER, WalletType.COURIER)
    order = Order(
        customer_id=customer.id,
        courier_id=courier.id,
        delivery_city="Jeddah",
        delivery_location=WKTElement(f"POINT({_LNG} {_LAT})", srid=4326),
        delivery_date=datetime.now(UTC).date() + timedelta(days=10),
        status=status,
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
        amount_from_wallet=Decimal("724.50"),
    )
    db.add(invoice)
    await db.flush()

    # Fund escrow (gateway -> escrow) so it holds the total to release.
    repo = WalletRepository(db)
    money = MoneyService(repo)
    gateway = await repo.get_system(WalletType.SYSTEM_GATEWAY)
    escrow = await repo.get_system(WalletType.SYSTEM_ESCROW)
    await money.post_group(
        correlation_id=uuid.uuid4(),
        legs=[
            Leg(wallet_id=gateway.id, amount=Decimal("-724.50"), txn_type=TransactionType.PAYMENT),
            Leg(wallet_id=escrow.id, amount=Decimal("724.50"), txn_type=TransactionType.PAYMENT),
        ],
    )
    return customer, courier, order, invoice


async def _proof_key(db: AsyncSession) -> str:
    """Register a delivery-proof key in the fake storage so confirm() passes."""
    media = MediaService(FakeStorageClient(Environment.TEST), _settings())
    _url, key, _ttl = await media.request_upload_url(
        purpose="DELIVERY_PROOF", content_type="image/jpeg", byte_size=1000
    )
    return key


async def test_deliver_requires_geofence(db_session: AsyncSession) -> None:
    _cust, courier, order, _inv = await _paid_order(db_session)
    svc = _service(db_session)
    key = await _proof_key(db_session)
    # Far from the drop-off (different city) -> rejected.
    with pytest.raises(ValidationDomainError):
        await svc.submit_delivery(
            order_id=order.id,
            courier_id=courier.id,
            data=DeliveryInput(latitude=24.7, longitude=46.7, proof_media_keys=[key], note=None),
        )


async def test_deliver_and_approve_releases_escrow(db_session: AsyncSession) -> None:
    customer, courier, order, _inv = await _paid_order(db_session)
    svc = _service(db_session)
    repo = WalletRepository(db_session)

    # The fake storage client used by the service must know the proof key; request it
    # through the service's own media client so confirm() sees it uploaded.
    _url, key, _ttl = await svc._media.request_upload_url(  # type: ignore[attr-defined]
        purpose="DELIVERY_PROOF", content_type="image/jpeg", byte_size=1000
    )
    delivered = await svc.submit_delivery(
        order_id=order.id,
        courier_id=courier.id,
        data=DeliveryInput(
            latitude=_LAT, longitude=_LNG, proof_media_keys=[key], note="left at door"
        ),
    )
    assert delivered.status is OrderStatus.DELIVERED

    approved = await svc.approve_order(order_id=order.id, customer_id=customer.id)
    assert approved.status is OrderStatus.COMPLETED
    assert approved.commission_amount == Decimal("60.00")
    assert approved.courier_payout_amount == Decimal("540.00")

    courier_wallet = await repo.get_by_user(courier.id)
    assert courier_wallet is not None and courier_wallet.balance == Decimal("540.00")
    report = await MoneyService(repo).reconcile()
    assert report.ok, report.drifts


async def test_double_approve_is_rejected(db_session: AsyncSession) -> None:
    customer, _courier, order, _inv = await _paid_order(db_session, status=OrderStatus.DELIVERED)
    svc = _service(db_session)
    await svc.approve_order(order_id=order.id, customer_id=customer.id)
    with pytest.raises(InvalidStateTransitionError):
        await svc.approve_order(order_id=order.id, customer_id=customer.id)


async def test_auto_approve_completes_overdue(db_session: AsyncSession) -> None:
    customer, courier, order, _inv = await _paid_order(db_session, status=OrderStatus.DELIVERED)
    order.delivered_at = datetime.now(UTC) - timedelta(hours=100)
    await db_session.flush()
    svc = _service(db_session)
    assert await svc.auto_approve(order_id=order.id) is True
    assert order.status is OrderStatus.COMPLETED
    # A second run is a no-op (already completed).
    assert await svc.auto_approve(order_id=order.id) is False


async def test_dispute_raise_then_resolve_customer_refunds(db_session: AsyncSession) -> None:
    customer, _courier, order, invoice = await _paid_order(db_session)
    svc = _service(db_session)
    dispute = await svc.raise_dispute(
        order_id=order.id, actor_id=customer.id, reason="Item never arrived"
    )
    assert order.status is OrderStatus.DISPUTED
    # A second dispute conflicts.
    with pytest.raises(ConflictError):
        await svc.raise_dispute(order_id=order.id, actor_id=customer.id, reason="again")

    admin = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.ADMIN)
    db_session.add(admin)
    await db_session.flush()
    await svc.resolve_dispute(
        dispute_id=dispute.id,
        admin_id=admin.id,
        outcome=DisputeStatus.RESOLVED_CUSTOMER,
        note="refunded",
    )
    assert order.status is OrderStatus.REFUNDED
    repo = WalletRepository(db_session)
    customer_wallet = await repo.get_by_user(customer.id)
    assert customer_wallet is not None and customer_wallet.balance == Decimal("724.50")
    assert (await MoneyService(repo).reconcile()).ok


async def test_resolve_courier_pays_out(db_session: AsyncSession) -> None:
    customer, courier, order, _inv = await _paid_order(db_session)
    svc = _service(db_session)
    dispute = await svc.raise_dispute(order_id=order.id, actor_id=courier.id, reason="dispute")
    admin, _w = await _user_wallet(db_session, UserRole.ADMIN, WalletType.CUSTOMER)
    await svc.resolve_dispute(
        dispute_id=dispute.id,
        admin_id=admin.id,
        outcome=DisputeStatus.RESOLVED_COURIER,
        note=None,
    )
    assert order.status is OrderStatus.COMPLETED
    repo = WalletRepository(db_session)
    courier_wallet = await repo.get_by_user(courier.id)
    assert courier_wallet is not None and courier_wallet.balance == Decimal("540.00")
    assert (await MoneyService(repo).reconcile()).ok


async def test_resolve_split_divides_escrow(db_session: AsyncSession) -> None:
    customer, courier, order, _inv = await _paid_order(db_session)
    svc = _service(db_session)
    dispute = await svc.raise_dispute(order_id=order.id, actor_id=customer.id, reason="partial")
    admin, _w = await _user_wallet(db_session, UserRole.ADMIN, WalletType.CUSTOMER)
    await svc.resolve_dispute(
        dispute_id=dispute.id,
        admin_id=admin.id,
        outcome=DisputeStatus.RESOLVED_SPLIT,
        note="split",
        courier_amount=Decimal("400.00"),
    )
    assert order.status is OrderStatus.COMPLETED
    repo = WalletRepository(db_session)
    courier_wallet = await repo.get_by_user(courier.id)
    customer_wallet = await repo.get_by_user(customer.id)
    assert courier_wallet is not None and courier_wallet.balance == Decimal("400.00")
    assert customer_wallet is not None and customer_wallet.balance == Decimal("324.50")
    assert (await MoneyService(repo).reconcile()).ok


async def test_resolve_split_requires_valid_amount(db_session: AsyncSession) -> None:
    customer, _courier, order, _inv = await _paid_order(db_session)
    svc = _service(db_session)
    dispute = await svc.raise_dispute(order_id=order.id, actor_id=customer.id, reason="x")
    admin, _w = await _user_wallet(db_session, UserRole.ADMIN, WalletType.CUSTOMER)
    with pytest.raises(ValidationDomainError):
        await svc.resolve_dispute(
            dispute_id=dispute.id,
            admin_id=admin.id,
            outcome=DisputeStatus.RESOLVED_SPLIT,
            note=None,
            courier_amount=Decimal("999999.00"),  # exceeds the total
        )
