"""Courier withdrawal workflow over held wallet funds and the append-only ledger."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.crypto import build_aad, build_cipher
from app.core.exceptions import (
    InvalidStateTransitionError,
    NotFoundError,
    ValidationDomainError,
)
from app.core.money import quantize_money
from app.models import Withdrawal
from app.models.enums import WithdrawalStatus
from app.repositories.audit_repository import AuditRepository
from app.repositories.wallet_repository import WalletRepository
from app.repositories.withdrawal_repository import WithdrawalRepository
from app.services.money_service import MoneyService


class WithdrawalService:
    """Requests, approves, rejects, and settles courier withdrawals."""

    def __init__(
        self,
        *,
        withdrawals: WithdrawalRepository,
        wallets: WalletRepository,
        money: MoneyService,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        """Bind repositories, ledger service, and validated settings."""
        self._withdrawals = withdrawals
        self._wallets = wallets
        self._money = money
        self._audit = audit
        self._settings = settings

    async def request_withdrawal(
        self,
        *,
        courier_id: uuid.UUID,
        amount: Decimal,
        iban: str,
        idempotency_key: str,
    ) -> Withdrawal:
        """Reserve courier funds and persist an encrypted payout request."""
        existing = await self._withdrawals.get_by_idempotency(
            courier_id=courier_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            return existing
        amount = quantize_money(amount)
        if not (
            self._settings.MIN_WITHDRAWAL_AMOUNT <= amount <= self._settings.MAX_WITHDRAWAL_AMOUNT
        ):
            raise ValidationDomainError("Withdrawal amount is outside the allowed range.")
        wallet = await self._wallets.get_by_user(courier_id)
        if wallet is None:
            raise NotFoundError("Wallet not found.")

        withdrawal_id = uuid.uuid4()
        cipher = build_cipher(
            self._settings.encryption_keys(), self._settings.FIELD_ENCRYPTION_KEY_VERSION
        )
        encrypted = cipher.encrypt(iban, build_aad("withdrawals", "iban", str(withdrawal_id)))
        try:
            async with self._withdrawals.savepoint():
                await self._money.hold_funds(wallet_id=wallet.id, amount=amount)
                return await self._withdrawals.create(
                    withdrawal_id=withdrawal_id,
                    courier_id=courier_id,
                    wallet_id=wallet.id,
                    amount=amount,
                    iban_encrypted=encrypted,
                    iban_last4=iban[-4:],
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            replay = await self._withdrawals.get_by_idempotency(
                courier_id=courier_id, idempotency_key=idempotency_key
            )
            if replay is not None:
                return replay
            raise

    async def approve(
        self, *, withdrawal_id: uuid.UUID, admin_id: uuid.UUID, ip: str | None
    ) -> Withdrawal:
        """Approve a requested withdrawal while retaining its funds hold."""
        row = await self._get_locked(withdrawal_id)
        if row.status is WithdrawalStatus.APPROVED:
            return row
        if row.status is not WithdrawalStatus.REQUESTED:
            raise InvalidStateTransitionError("Only requested withdrawals may be approved.")
        await self._withdrawals.set_status(row, status=WithdrawalStatus.APPROVED, admin_id=admin_id)
        await self._record(admin_id=admin_id, row=row, action="WITHDRAWAL_APPROVE", ip=ip)
        return row

    async def reject(
        self,
        *,
        withdrawal_id: uuid.UUID,
        admin_id: uuid.UUID,
        reason: str,
        ip: str | None,
    ) -> Withdrawal:
        """Reject a pending withdrawal and release its reserved funds."""
        row = await self._get_locked(withdrawal_id)
        if row.status is WithdrawalStatus.REJECTED:
            return row
        if row.status not in (WithdrawalStatus.REQUESTED, WithdrawalStatus.APPROVED):
            raise InvalidStateTransitionError("This withdrawal can no longer be rejected.")
        await self._money.release_hold(wallet_id=row.wallet_id, amount=row.amount)
        await self._withdrawals.set_status(
            row,
            status=WithdrawalStatus.REJECTED,
            admin_id=admin_id,
            rejection_reason=reason,
        )
        await self._record(admin_id=admin_id, row=row, action="WITHDRAWAL_REJECT", ip=ip)
        return row

    async def mark_paid(
        self, *, withdrawal_id: uuid.UUID, admin_id: uuid.UUID, ip: str | None
    ) -> Withdrawal:
        """Settle an approved external payout through the double-entry ledger."""
        row = await self._get_locked(withdrawal_id)
        if row.status is WithdrawalStatus.PAID:
            return row
        if row.status is not WithdrawalStatus.APPROVED:
            raise InvalidStateTransitionError("Only approved withdrawals may be paid.")
        posted = await self._money.pay_withdrawal(
            courier_wallet_id=row.wallet_id,
            amount=row.amount,
            withdrawal_id=row.id,
        )
        if not posted:
            raise InvalidStateTransitionError("This withdrawal was already paid.")
        await self._withdrawals.set_status(row, status=WithdrawalStatus.PAID, admin_id=admin_id)
        await self._record(admin_id=admin_id, row=row, action="WITHDRAWAL_PAID", ip=ip)
        return row

    async def _get_locked(self, withdrawal_id: uuid.UUID) -> Withdrawal:
        row = await self._withdrawals.get_for_update(withdrawal_id)
        if row is None:
            raise NotFoundError("Withdrawal not found.")
        return row

    async def _record(
        self,
        *,
        admin_id: uuid.UUID,
        row: Withdrawal,
        action: str,
        ip: str | None,
    ) -> None:
        await self._audit.record(
            actor_user_id=admin_id,
            action=action,
            entity_type="withdrawals",
            entity_id=row.id,
            ip_address=ip,
        )
