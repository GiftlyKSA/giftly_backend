"""Tests for the money/ledger service (SPEC SECTION 20, 24).

Covers the double-entry primitive, the golden top-up and approval-payout groups, the
non-negative overdraft guard, reconciliation, a ledger property test (random balanced
groups keep every wallet's balance equal to its settled sum and every correlation group
summed to zero), and a concurrency test (parallel idempotent posts credit exactly once).
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from app.core.config import Settings
from app.core.db import build_engine, build_session_factory
from app.models import PaymentIntent, User, Wallet
from app.models.enums import (
    PaymentIntentStatus,
    PaymentPurpose,
    TransactionStatus,
    TransactionType,
    UserRole,
    WalletType,
)
from app.repositories.wallet_repository import WalletRepository
from app.services.money_service import LedgerImbalanceError, Leg, MoneyService
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import make_test_settings


def _settings() -> Settings:
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if os.environ.get("REDIS_URL"):
        overrides["REDIS_URL"] = os.environ["REDIS_URL"]
    return make_test_settings(**overrides)


async def _make_user_wallet(db: AsyncSession, wtype: WalletType) -> Wallet:
    user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    db.add(user)
    await db.flush()
    wallet = Wallet(user_id=user.id, type=wtype)
    db.add(wallet)
    await db.flush()
    return wallet


async def _make_topup_intent(
    db: AsyncSession, user_id: uuid.UUID, amount: Decimal
) -> PaymentIntent:
    intent = PaymentIntent(
        user_id=user_id,
        purpose=PaymentPurpose.WALLET_TOPUP,
        amount=amount,
        status=PaymentIntentStatus.NEW,
        expires_at=datetime.now(UTC) + timedelta(hours=48),
    )
    db.add(intent)
    await db.flush()
    return intent


async def test_credit_topup_golden(db_session: AsyncSession) -> None:
    repo = WalletRepository(db_session)
    service = MoneyService(repo)
    wallet = await _make_user_wallet(db_session, WalletType.CUSTOMER)
    gateway = await repo.get_system(WalletType.SYSTEM_GATEWAY)
    before_gateway = gateway.balance
    intent = await _make_topup_intent(db_session, wallet.user_id, Decimal("300.00"))

    posted = await service.credit_topup(
        user_wallet_id=wallet.id, amount=Decimal("300.00"), intent_id=intent.id
    )
    assert posted is True
    await db_session.refresh(wallet)
    await db_session.refresh(gateway)
    assert wallet.balance == Decimal("300.00")
    assert gateway.balance == before_gateway - Decimal("300.00")


async def test_topup_is_idempotent(db_session: AsyncSession) -> None:
    service = MoneyService(WalletRepository(db_session))
    wallet = await _make_user_wallet(db_session, WalletType.CUSTOMER)
    intent = await _make_topup_intent(db_session, wallet.user_id, Decimal("300.00"))
    assert (
        await service.credit_topup(
            user_wallet_id=wallet.id, amount=Decimal("300.00"), intent_id=intent.id
        )
        is True
    )
    # Replaying the same intent is a no-op.
    assert (
        await service.credit_topup(
            user_wallet_id=wallet.id, amount=Decimal("300.00"), intent_id=intent.id
        )
        is False
    )
    await db_session.refresh(wallet)
    assert wallet.balance == Decimal("300.00")


async def test_approval_payout_group_golden(db_session: AsyncSession) -> None:
    repo = WalletRepository(db_session)
    service = MoneyService(repo)
    escrow = await repo.get_system(WalletType.SYSTEM_ESCROW)
    tax = await repo.get_system(WalletType.SYSTEM_TAX_PAYABLE)
    revenue = await repo.get_system(WalletType.SYSTEM_REVENUE)
    gateway = await repo.get_system(WalletType.SYSTEM_GATEWAY)
    courier = await _make_user_wallet(db_session, WalletType.COURIER)

    # Fund escrow (gateway -> escrow) so it holds the 655.50 to release.
    await service.post_group(
        correlation_id=uuid.uuid4(),
        legs=[
            Leg(wallet_id=gateway.id, amount=Decimal("-655.50"), txn_type=TransactionType.PAYMENT),
            Leg(wallet_id=escrow.id, amount=Decimal("655.50"), txn_type=TransactionType.PAYMENT),
        ],
    )
    await db_session.refresh(escrow)
    escrow_start = escrow.balance

    # Approval payout (workflow G): escrow -655.50 -> tax 85.50 + courier 540 + revenue 30.
    await service.post_group(
        correlation_id=uuid.uuid4(),
        legs=[
            Leg(
                wallet_id=escrow.id,
                amount=Decimal("-655.50"),
                txn_type=TransactionType.ESCROW_RELEASE,
            ),
            Leg(wallet_id=tax.id, amount=Decimal("85.50"), txn_type=TransactionType.TAX),
            Leg(
                wallet_id=courier.id,
                amount=Decimal("540.00"),
                txn_type=TransactionType.ESCROW_RELEASE,
            ),
            Leg(wallet_id=revenue.id, amount=Decimal("30.00"), txn_type=TransactionType.COMMISSION),
        ],
    )
    await db_session.refresh(escrow)
    await db_session.refresh(courier)
    assert escrow.balance == escrow_start - Decimal("655.50")
    assert courier.balance == Decimal("540.00")

    report = await service.reconcile()
    assert report.ok, report.drifts


async def test_imbalanced_group_is_rejected(db_session: AsyncSession) -> None:
    repo = WalletRepository(db_session)
    service = MoneyService(repo)
    wallet = await _make_user_wallet(db_session, WalletType.CUSTOMER)
    gateway = await repo.get_system(WalletType.SYSTEM_GATEWAY)
    with pytest.raises(LedgerImbalanceError):
        await service.post_group(
            correlation_id=uuid.uuid4(),
            legs=[
                Leg(wallet_id=wallet.id, amount=Decimal("300.00"), txn_type=TransactionType.TOPUP),
                Leg(
                    wallet_id=gateway.id, amount=Decimal("-299.00"), txn_type=TransactionType.TOPUP
                ),
            ],
        )


async def test_overdraft_of_user_wallet_is_blocked(db_session: AsyncSession) -> None:
    repo = WalletRepository(db_session)
    service = MoneyService(repo)
    wallet = await _make_user_wallet(db_session, WalletType.CUSTOMER)
    gateway = await repo.get_system(WalletType.SYSTEM_GATEWAY)
    # Debiting a user wallet below zero violates chk_balance_non_negative.
    with pytest.raises(IntegrityError):
        await service.post_group(
            correlation_id=uuid.uuid4(),
            legs=[
                Leg(
                    wallet_id=wallet.id, amount=Decimal("-50.00"), txn_type=TransactionType.PAYMENT
                ),
                Leg(
                    wallet_id=gateway.id, amount=Decimal("50.00"), txn_type=TransactionType.PAYMENT
                ),
            ],
        )


async def test_ledger_property_random_groups_keep_invariants(db_session: AsyncSession) -> None:
    repo = WalletRepository(db_session)
    service = MoneyService(repo)
    gateway = await repo.get_system(WalletType.SYSTEM_GATEWAY)
    a = await _make_user_wallet(db_session, WalletType.CUSTOMER)
    b = await _make_user_wallet(db_session, WalletType.CUSTOMER)
    rng = random.Random(1234)

    for _ in range(40):
        amount = Decimal(rng.randint(1, 5000)) / Decimal(100)
        target = rng.choice([a, b])
        # Bound transfers by the live DB balance so a user wallet never goes negative
        # (post_group updates the wallet object in place, so target.balance is current).
        if rng.random() < 0.5 or target.balance < amount:
            await service.post_group(
                correlation_id=uuid.uuid4(),
                legs=[
                    Leg(wallet_id=gateway.id, amount=-amount, txn_type=TransactionType.TOPUP),
                    Leg(wallet_id=target.id, amount=amount, txn_type=TransactionType.TOPUP),
                ],
            )
        else:
            other = b if target is a else a
            await service.post_group(
                correlation_id=uuid.uuid4(),
                legs=[
                    Leg(wallet_id=target.id, amount=-amount, txn_type=TransactionType.PAYMENT),
                    Leg(wallet_id=other.id, amount=amount, txn_type=TransactionType.PAYMENT),
                ],
            )

    # The invariant: every wallet's balance equals the sum of its settled transactions.
    for wallet in (a, b, gateway):
        await db_session.refresh(wallet)
        assert wallet.balance == await repo.settled_balance(wallet.id)
    report = await service.reconcile()
    assert report.ok, report.drifts


# --- Concurrency: parallel idempotent posts credit exactly once -------------


@pytest_asyncio.fixture
async def committing_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = _settings()
    engine = build_engine(settings)
    try:
        async with engine.connect() as conn:
            await conn.rollback()
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")
    yield build_session_factory(engine)
    await engine.dispose()


async def test_parallel_topups_credit_exactly_once(
    committing_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Seed a committed user, wallet, and top-up intent.
    async with committing_factory() as s:
        user = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
        s.add(user)
        await s.flush()
        wallet = Wallet(user_id=user.id, type=WalletType.CUSTOMER)
        s.add(wallet)
        await s.flush()
        intent = PaymentIntent(
            user_id=user.id,
            purpose=PaymentPurpose.WALLET_TOPUP,
            amount=Decimal("300.00"),
            status=PaymentIntentStatus.NEW,
            expires_at=datetime.now(UTC) + timedelta(hours=48),
        )
        s.add(intent)
        await s.commit()
        wallet_id = wallet.id
        user_id = user.id
        intent_id = intent.id

    async def attempt() -> bool:
        async with committing_factory() as session:
            service = MoneyService(WalletRepository(session))
            try:
                posted = await service.credit_topup(
                    user_wallet_id=wallet_id, amount=Decimal("300.00"), intent_id=intent_id
                )
                await session.commit()
                return posted
            except IntegrityError:
                await session.rollback()
                return False

    results = await asyncio.gather(*[attempt() for _ in range(10)])
    # Exactly one attempt actually posted the credit; the rest were replays or lost the
    # unique-key race. The committed group is balanced, so it is left in place (the
    # ledger is append-only — its rows must never be deleted).
    assert sum(1 for r in results if r) == 1
    async with committing_factory() as s:
        w = await s.get(Wallet, wallet_id)
        assert w is not None
        assert w.balance == Decimal("300.00")
        report = await MoneyService(WalletRepository(s)).reconcile()
        assert report.ok, report.drifts
    # user_id is retained by the committed (balanced) ledger rows; nothing to clean up.
    _ = user_id


async def test_available_balance_and_no_wallet(db_session: AsyncSession) -> None:
    service = MoneyService(WalletRepository(db_session))
    wallet = await _make_user_wallet(db_session, WalletType.CUSTOMER)
    assert await service.available_balance(wallet.user_id) == Decimal("0.00")
    # A user with no wallet reports zero available.
    assert await service.available_balance(uuid.uuid4()) == Decimal("0.00")


async def test_reconcile_detects_drift(db_session: AsyncSession) -> None:
    repo = WalletRepository(db_session)
    service = MoneyService(repo)
    wallet = await _make_user_wallet(db_session, WalletType.CUSTOMER)
    # Append a lone SETTLED entry WITHOUT updating the wallet balance -> the invariant
    # (balance == sum of settled) and the correlation zero-sum are both violated.
    repo.append_transaction(
        wallet_id=wallet.id,
        amount=Decimal("10.00"),
        txn_type=TransactionType.TOPUP,
        status=TransactionStatus.SETTLED,
        correlation_id=uuid.uuid4(),
        balance_after=Decimal("10.00"),
    )
    await db_session.flush()
    report = await service.reconcile()
    assert not report.ok
    assert any("wallet" in d for d in report.drifts)


async def test_run_reconciliation_job(committing_factory: async_sessionmaker[AsyncSession]) -> None:
    # Uses the committed DB; the run builds its own engine from settings.
    from app.workers.reconciliation import run_reconciliation

    report = await run_reconciliation()
    assert report.wallets_checked >= 4  # at least the four system wallets
