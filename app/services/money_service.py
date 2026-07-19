"""The money/ledger service — the only code that moves money (SPEC SECTION 10, 20).

Every movement is a double-entry group: >= 2 legs sharing one ``correlation_id`` whose
signed amounts sum to exactly 0.00, posted atomically. Wallets are locked FOR UPDATE in
ascending id order to prevent deadlocks, and the zero-sum invariant is asserted at
runtime before the write — money is never created or destroyed, only moved. External
flows balance against SYSTEM_GATEWAY; VAT accrues to SYSTEM_TAX_PAYABLE.

The wallet invariant ``balance == SUM(settled amounts)`` and the per-correlation
zero-sum invariant are re-checked by :meth:`reconcile`, which pages on any drift.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.exceptions import InsufficientFundsError
from app.core.money import ZERO, quantize_money
from app.models.enums import TransactionStatus, TransactionType, WalletType
from app.repositories.wallet_repository import WalletRepository


class LedgerImbalanceError(Exception):
    """Raised when a ledger group's signed amounts do not sum to exactly zero."""


class ReconciliationError(Exception):
    """Raised when a wallet balance or correlation group violates its invariant."""


@dataclass(frozen=True)
class Leg:
    """One side of a double-entry movement.

    Attributes:
        wallet_id: The wallet this leg debits (negative) or credits (positive).
        amount: Signed money amount; must be non-zero.
        txn_type: The economic meaning of the entry.
        idempotency_key: Optional unique key; a replay with the same key is a no-op.
        reference_order_id / reference_invoice_id / reference_intent_id: Optional links.
        description: Optional human-readable note.
    """

    wallet_id: uuid.UUID
    amount: Decimal
    txn_type: TransactionType
    idempotency_key: str | None = None
    reference_order_id: uuid.UUID | None = None
    reference_invoice_id: uuid.UUID | None = None
    reference_intent_id: uuid.UUID | None = None
    description: str | None = None


@dataclass(frozen=True)
class ReconcileReport:
    """The outcome of a reconciliation pass."""

    wallets_checked: int
    correlations_checked: int
    drifts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no drift was found."""
        return not self.drifts


class MoneyService:
    """Posts balanced ledger groups and reconciles the ledger."""

    def __init__(self, wallets: WalletRepository) -> None:
        """Bind the service to a wallet repository (and its session)."""
        self._wallets = wallets

    async def post_group(self, *, legs: list[Leg], correlation_id: uuid.UUID) -> bool:
        """Post a balanced double-entry group atomically.

        Locks every involved wallet FOR UPDATE (ascending id), applies each leg to the
        wallet balance, and appends one SETTLED ledger row per leg. Idempotent: if any
        leg's idempotency key is already present, the whole group is treated as already
        posted and nothing is written.

        Args:
            legs: Two or more legs whose signed amounts sum to 0.00.
            correlation_id: The shared id tying the legs into one movement.

        Returns:
            True if the group was posted, False if it was a detected replay (no-op).

        Raises:
            LedgerImbalanceError: The legs do not sum to zero, or a leg amount is zero,
                or there are fewer than two legs.
        """
        if len(legs) < 2:
            raise LedgerImbalanceError("A ledger group needs at least two legs.")
        total = sum((leg.amount for leg in legs), ZERO)
        if quantize_money(total) != ZERO:
            raise LedgerImbalanceError(f"Ledger group does not sum to zero (got {total}).")
        for leg in legs:
            if quantize_money(leg.amount) == ZERO:
                raise LedgerImbalanceError("A ledger leg amount must be non-zero.")

        # Idempotency: a replay is detected by any leg's idempotency key already existing.
        for leg in legs:
            if leg.idempotency_key and await self._wallets.idempotency_key_exists(
                leg.idempotency_key
            ):
                return False

        locked = await self._wallets.lock_wallets([leg.wallet_id for leg in legs])
        for leg in legs:
            wallet = locked[leg.wallet_id]
            wallet.balance = quantize_money(wallet.balance + leg.amount)
            wallet.version += 1
            self._wallets.append_transaction(
                wallet_id=leg.wallet_id,
                amount=quantize_money(leg.amount),
                txn_type=leg.txn_type,
                status=TransactionStatus.SETTLED,
                correlation_id=correlation_id,
                balance_after=wallet.balance,
                idempotency_key=leg.idempotency_key,
                reference_order_id=leg.reference_order_id,
                reference_invoice_id=leg.reference_invoice_id,
                reference_intent_id=leg.reference_intent_id,
                description=leg.description,
            )
        # Flush so this group is durable before any later group's FOR UPDATE reload:
        # a `SELECT ... FOR UPDATE` repopulates wallet rows and would otherwise discard
        # these still-pending in-memory balance updates.
        await self._wallets.flush()
        return True

    async def credit_topup(
        self,
        *,
        user_wallet_id: uuid.UUID,
        amount: Decimal,
        intent_id: uuid.UUID,
    ) -> bool:
        """Credit a paid wallet top-up, balanced against SYSTEM_GATEWAY (workflow B.7)."""
        gateway = await self._wallets.get_system(WalletType.SYSTEM_GATEWAY)
        correlation = uuid.uuid4()
        return await self.post_group(
            correlation_id=correlation,
            legs=[
                Leg(
                    wallet_id=user_wallet_id,
                    amount=quantize_money(amount),
                    txn_type=TransactionType.TOPUP,
                    idempotency_key=f"intent:{intent_id}:topup",
                    reference_intent_id=intent_id,
                ),
                Leg(
                    wallet_id=gateway.id,
                    amount=quantize_money(-amount),
                    txn_type=TransactionType.TOPUP,
                    reference_intent_id=intent_id,
                ),
            ],
        )

    async def release_escrow_on_completion(
        self,
        *,
        order_id: uuid.UUID,
        invoice_id: uuid.UUID,
        courier_wallet_id: uuid.UUID,
        courier_payout_amount: Decimal,
        tax_amount: Decimal,
        platform_revenue_amount: Decimal,
    ) -> bool:
        """Release escrow on order completion (workflow G), idempotently.

        Escrow pays out the courier, the tax authority, and platform revenue in one
        balanced group (``-total + payout + tax + revenue == 0``). Keyed on the order so a
        customer approval racing the auto-approve job releases exactly once.
        """
        courier_payout = quantize_money(courier_payout_amount)
        tax = quantize_money(tax_amount)
        revenue = quantize_money(platform_revenue_amount)
        total = quantize_money(courier_payout + tax + revenue)
        escrow = await self._wallets.get_system(WalletType.SYSTEM_ESCROW)
        tax_wallet = await self._wallets.get_system(WalletType.SYSTEM_TAX_PAYABLE)
        revenue_wallet = await self._wallets.get_system(WalletType.SYSTEM_REVENUE)
        correlation = uuid.uuid4()

        legs = [
            Leg(
                wallet_id=escrow.id,
                amount=-total,
                txn_type=TransactionType.ESCROW_RELEASE,
                idempotency_key=f"order:{order_id}:release",
                reference_order_id=order_id,
                reference_invoice_id=invoice_id,
            ),
            Leg(
                wallet_id=courier_wallet_id,
                amount=courier_payout,
                txn_type=TransactionType.ESCROW_RELEASE,
                reference_order_id=order_id,
                reference_invoice_id=invoice_id,
            ),
        ]
        if tax != ZERO:
            legs.append(
                Leg(
                    wallet_id=tax_wallet.id,
                    amount=tax,
                    txn_type=TransactionType.TAX,
                    reference_order_id=order_id,
                    reference_invoice_id=invoice_id,
                )
            )
        if revenue != ZERO:
            legs.append(
                Leg(
                    wallet_id=revenue_wallet.id,
                    amount=revenue,
                    txn_type=TransactionType.COMMISSION,
                    reference_order_id=order_id,
                    reference_invoice_id=invoice_id,
                )
            )
        return await self.post_group(legs=legs, correlation_id=correlation)

    async def refund_escrow(
        self,
        *,
        order_id: uuid.UUID,
        invoice_id: uuid.UUID,
        customer_wallet_id: uuid.UUID,
        amount: Decimal,
    ) -> bool:
        """Refund the full escrow total to the customer's wallet (dispute for customer)."""
        amount = quantize_money(amount)
        escrow = await self._wallets.get_system(WalletType.SYSTEM_ESCROW)
        return await self.post_group(
            correlation_id=uuid.uuid4(),
            legs=[
                Leg(
                    wallet_id=escrow.id,
                    amount=-amount,
                    txn_type=TransactionType.REFUND,
                    idempotency_key=f"order:{order_id}:refund",
                    reference_order_id=order_id,
                    reference_invoice_id=invoice_id,
                ),
                Leg(
                    wallet_id=customer_wallet_id,
                    amount=amount,
                    txn_type=TransactionType.REFUND,
                    reference_order_id=order_id,
                    reference_invoice_id=invoice_id,
                ),
            ],
        )

    async def split_escrow(
        self,
        *,
        order_id: uuid.UUID,
        invoice_id: uuid.UUID,
        courier_wallet_id: uuid.UUID,
        customer_wallet_id: uuid.UUID,
        courier_amount: Decimal,
        refund_amount: Decimal,
    ) -> bool:
        """Split escrow between courier and customer (dispute resolved split).

        The platform books no revenue or tax on a split — the two parties divide the held
        total (``courier_amount + refund_amount == escrow total``). Either side may be
        zero, in which case its leg is omitted.
        """
        courier_amount = quantize_money(courier_amount)
        refund_amount = quantize_money(refund_amount)
        total = quantize_money(courier_amount + refund_amount)
        escrow = await self._wallets.get_system(WalletType.SYSTEM_ESCROW)
        legs = [
            Leg(
                wallet_id=escrow.id,
                amount=-total,
                txn_type=TransactionType.ESCROW_RELEASE,
                idempotency_key=f"order:{order_id}:split",
                reference_order_id=order_id,
                reference_invoice_id=invoice_id,
            )
        ]
        if courier_amount != ZERO:
            legs.append(
                Leg(
                    wallet_id=courier_wallet_id,
                    amount=courier_amount,
                    txn_type=TransactionType.ESCROW_RELEASE,
                    reference_order_id=order_id,
                    reference_invoice_id=invoice_id,
                )
            )
        if refund_amount != ZERO:
            legs.append(
                Leg(
                    wallet_id=customer_wallet_id,
                    amount=refund_amount,
                    txn_type=TransactionType.REFUND,
                    reference_order_id=order_id,
                    reference_invoice_id=invoice_id,
                )
            )
        return await self.post_group(legs=legs, correlation_id=uuid.uuid4())

    async def available_balance(self, user_id: uuid.UUID) -> Decimal:
        """Return a user's available balance (balance - held), or 0 if no wallet."""
        wallet = await self._wallets.get_by_user(user_id)
        if wallet is None:
            return ZERO
        return quantize_money(wallet.balance - wallet.held_balance)

    async def hold_funds(self, *, wallet_id: uuid.UUID, amount: Decimal) -> None:
        """Reserve ``amount`` of a wallet's available balance (held, not moved).

        A hold is a reservation, not a ledger movement: it bumps ``held_balance`` under a
        row lock so the same balance cannot back two pending gateway payments. The wallet
        invariant (balance == SUM(settled)) is untouched.

        Raises:
            InsufficientFundsError: The available balance cannot cover the hold.
        """
        amount = quantize_money(amount)
        locked = await self._wallets.lock_wallets([wallet_id])
        wallet = locked[wallet_id]
        if quantize_money(wallet.balance - wallet.held_balance) < amount:
            raise InsufficientFundsError()
        wallet.held_balance = quantize_money(wallet.held_balance + amount)
        wallet.version += 1
        await self._wallets.flush()

    async def release_hold(self, *, wallet_id: uuid.UUID, amount: Decimal) -> None:
        """Release a previously placed hold, returning the amount to available."""
        amount = quantize_money(amount)
        locked = await self._wallets.lock_wallets([wallet_id])
        wallet = locked[wallet_id]
        wallet.held_balance = quantize_money(max(ZERO, wallet.held_balance - amount))
        wallet.version += 1
        await self._wallets.flush()

    async def fund_escrow_for_invoice(
        self,
        *,
        customer_wallet_id: uuid.UUID,
        wallet_amount: Decimal,
        gateway_amount: Decimal,
        invoice_id: uuid.UUID,
        order_id: uuid.UUID,
        intent_id: uuid.UUID | None,
        was_held: bool,
    ) -> bool:
        """Move an invoice's paid total into escrow (workflow B.8), idempotently.

        The customer's wallet portion and the gateway portion both land in
        ``SYSTEM_ESCROW``, where they are held until delivery/approval releases them
        (Phase 10). Balanced group: ``-wallet -gateway +total == 0``. The escrow leg
        carries the idempotency key, so a webhook replay is a no-op.

        When ``was_held`` is True the wallet portion was reserved by :meth:`hold_funds`
        (the split/gateway path); on the first successful post the hold is released, and
        never on a replay.

        Returns:
            True if the group was posted, False if it was a detected replay.
        """
        wallet_amount = quantize_money(wallet_amount)
        gateway_amount = quantize_money(gateway_amount)
        total = quantize_money(wallet_amount + gateway_amount)
        escrow = await self._wallets.get_system(WalletType.SYSTEM_ESCROW)
        gateway = await self._wallets.get_system(WalletType.SYSTEM_GATEWAY)
        correlation = uuid.uuid4()

        legs: list[Leg] = []
        if wallet_amount > ZERO:
            legs.append(
                Leg(
                    wallet_id=customer_wallet_id,
                    amount=-wallet_amount,
                    txn_type=TransactionType.PAYMENT,
                    reference_invoice_id=invoice_id,
                    reference_order_id=order_id,
                    reference_intent_id=intent_id,
                )
            )
        if gateway_amount > ZERO:
            legs.append(
                Leg(
                    wallet_id=gateway.id,
                    amount=-gateway_amount,
                    txn_type=TransactionType.PAYMENT,
                    reference_invoice_id=invoice_id,
                    reference_order_id=order_id,
                    reference_intent_id=intent_id,
                )
            )
        legs.append(
            Leg(
                wallet_id=escrow.id,
                amount=total,
                txn_type=TransactionType.ESCROW_HOLD,
                idempotency_key=f"invoice:{invoice_id}:escrow",
                reference_invoice_id=invoice_id,
                reference_order_id=order_id,
                reference_intent_id=intent_id,
            )
        )
        posted = await self.post_group(legs=legs, correlation_id=correlation)
        # Release the reservation only on the first successful post (never on a replay).
        if posted and was_held and wallet_amount > ZERO:
            await self.release_hold(wallet_id=customer_wallet_id, amount=wallet_amount)
        return posted

    async def reconcile(self) -> ReconcileReport:
        """Assert the ledger invariants across every wallet and correlation group.

        For each wallet, ``balance`` must equal the sum of its SETTLED transactions; for
        each correlation group, the SETTLED amounts must sum to 0.00. Returns a report
        listing any drift (the caller pages on a non-empty result).
        """
        drifts: list[str] = []
        wallets = await self._wallets.all_wallets()
        for wallet in wallets:
            settled = await self._wallets.settled_balance(wallet.id)
            if quantize_money(settled) != quantize_money(wallet.balance):
                drifts.append(
                    f"wallet {wallet.id} balance {wallet.balance} != settled sum {settled}"
                )
        sums = await self._wallets.correlation_settled_sums()
        for correlation_id, total in sums.items():
            if quantize_money(total) != ZERO:
                drifts.append(f"correlation {correlation_id} settled sum {total} != 0.00")
        return ReconcileReport(
            wallets_checked=len(wallets), correlations_checked=len(sums), drifts=drifts
        )
