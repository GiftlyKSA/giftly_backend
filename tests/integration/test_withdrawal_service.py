"""Courier withdrawal holds, transitions, encryption, and ledger settlement."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from app.core.exceptions import InsufficientFundsError
from app.models import User, Wallet
from app.models.enums import TransactionType, UserRole, WalletType, WithdrawalStatus
from app.repositories.audit_repository import AuditRepository
from app.repositories.wallet_repository import WalletRepository
from app.repositories.withdrawal_repository import WithdrawalRepository
from app.services.money_service import Leg, MoneyService
from app.services.withdrawal_service import WithdrawalService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_test_settings

_IBAN = "SA0380000000608010167519"


async def _actor(db: AsyncSession, role: UserRole) -> User:
    user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=role)
    db.add(user)
    await db.flush()
    return user


async def _service(db: AsyncSession) -> tuple[WithdrawalService, User, User, Wallet]:
    courier = await _actor(db, UserRole.COURIER)
    admin = await _actor(db, UserRole.ADMIN)
    wallet = Wallet(user_id=courier.id, type=WalletType.COURIER)
    db.add(wallet)
    await db.flush()

    wallets = WalletRepository(db)
    money = MoneyService(wallets)
    gateway = await wallets.get_system(WalletType.SYSTEM_GATEWAY)
    await money.post_group(
        correlation_id=uuid.uuid4(),
        legs=[
            Leg(
                wallet_id=wallet.id,
                amount=Decimal("500.00"),
                txn_type=TransactionType.TOPUP,
            ),
            Leg(
                wallet_id=gateway.id,
                amount=Decimal("-500.00"),
                txn_type=TransactionType.TOPUP,
            ),
        ],
    )
    return (
        WithdrawalService(
            withdrawals=WithdrawalRepository(db),
            wallets=wallets,
            money=money,
            audit=AuditRepository(db),
            settings=make_test_settings(),
        ),
        courier,
        admin,
        wallet,
    )


async def test_withdrawal_request_and_paid_flow_preserves_ledger(
    db_session: AsyncSession,
) -> None:
    service, courier, admin, wallet = await _service(db_session)

    withdrawal = await service.request_withdrawal(
        courier_id=courier.id,
        amount=Decimal("200.00"),
        iban=_IBAN,
        idempotency_key=str(uuid.uuid4()),
    )
    assert withdrawal.status is WithdrawalStatus.REQUESTED
    assert withdrawal.iban_last4 == "7519"
    assert _IBAN not in withdrawal.iban_encrypted
    assert wallet.balance == Decimal("500.00")
    assert wallet.held_balance == Decimal("200.00")

    await service.approve(withdrawal_id=withdrawal.id, admin_id=admin.id, ip="127.0.0.1")
    assert withdrawal.status is WithdrawalStatus.APPROVED
    await service.mark_paid(withdrawal_id=withdrawal.id, admin_id=admin.id, ip="127.0.0.1")

    assert withdrawal.status is WithdrawalStatus.PAID
    assert wallet.balance == Decimal("300.00")
    assert wallet.held_balance == Decimal("0.00")
    assert (await MoneyService(WalletRepository(db_session)).reconcile()).ok
    replay = await service.mark_paid(withdrawal_id=withdrawal.id, admin_id=admin.id, ip="127.0.0.1")
    assert replay.status is WithdrawalStatus.PAID


async def test_reject_withdrawal_releases_hold(db_session: AsyncSession) -> None:
    service, courier, admin, wallet = await _service(db_session)
    withdrawal = await service.request_withdrawal(
        courier_id=courier.id,
        amount=Decimal("100.00"),
        iban=_IBAN,
        idempotency_key=str(uuid.uuid4()),
    )

    await service.reject(
        withdrawal_id=withdrawal.id,
        admin_id=admin.id,
        reason="Bank details could not be verified.",
        ip=None,
    )

    assert withdrawal.status is WithdrawalStatus.REJECTED
    assert wallet.balance == Decimal("500.00")
    assert wallet.held_balance == Decimal("0.00")


async def test_withdrawal_rejects_unavailable_funds(db_session: AsyncSession) -> None:
    service, courier, _admin, _wallet = await _service(db_session)

    with pytest.raises(InsufficientFundsError):
        await service.request_withdrawal(
            courier_id=courier.id,
            amount=Decimal("600.00"),
            iban=_IBAN,
            idempotency_key=str(uuid.uuid4()),
        )


async def test_withdrawal_request_replay_does_not_duplicate_hold(
    db_session: AsyncSession,
) -> None:
    service, courier, _admin, wallet = await _service(db_session)
    key = str(uuid.uuid4())

    first = await service.request_withdrawal(
        courier_id=courier.id,
        amount=Decimal("100.00"),
        iban=_IBAN,
        idempotency_key=key,
    )
    replay = await service.request_withdrawal(
        courier_id=courier.id,
        amount=Decimal("100.00"),
        iban=_IBAN,
        idempotency_key=key,
    )

    assert replay.id == first.id
    assert wallet.held_balance == Decimal("100.00")
