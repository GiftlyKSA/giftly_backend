"""Payment-intent and wallet-top-up persistence (SPEC SECTION 5.1, ADR 0003).

A single ``payment_intents`` row is the only gateway-facing record, discriminated by
``purpose``. The webhook does ONE lookup by ``paylink_transaction_no`` and dispatches on
purpose — the ambiguity that produces double-credits is designed out.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PaymentIntent, WalletTopup
from app.models.enums import PaymentIntentStatus, PaymentPurpose


class PaymentRepository:
    """Creates and reads payment intents and top-ups, with FOR UPDATE on settle."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def create_intent(
        self,
        *,
        user_id: uuid.UUID,
        purpose: PaymentPurpose,
        amount: Decimal,
        reference_invoice_id: uuid.UUID | None,
        expires_at: datetime,
    ) -> PaymentIntent:
        """Insert a NEW payment intent for a top-up or an invoice remainder."""
        intent = PaymentIntent(
            user_id=user_id,
            purpose=purpose,
            amount=amount,
            status=PaymentIntentStatus.NEW,
            reference_invoice_id=reference_invoice_id,
            expires_at=expires_at,
        )
        self._session.add(intent)
        await self._session.flush()
        return intent

    async def attach_gateway(self, intent: PaymentIntent, *, transaction_no: str, url: str) -> None:
        """Record the gateway's transaction number and payment URL on the intent."""
        intent.paylink_transaction_no = transaction_no
        intent.paylink_url = url
        await self._session.flush()

    async def create_topup(
        self,
        *,
        user_id: uuid.UUID,
        wallet_id: uuid.UUID,
        payment_intent_id: uuid.UUID,
        amount: Decimal,
    ) -> WalletTopup:
        """Insert the wallet-top-up row tied to its intent (1:1)."""
        topup = WalletTopup(
            user_id=user_id,
            wallet_id=wallet_id,
            payment_intent_id=payment_intent_id,
            amount=amount,
        )
        self._session.add(topup)
        await self._session.flush()
        return topup

    async def get_intent(self, intent_id: uuid.UUID) -> PaymentIntent | None:
        """Return a payment intent by id, or None."""
        return await self._session.get(PaymentIntent, intent_id)

    async def lock_intent_by_txn(self, transaction_no: str) -> PaymentIntent | None:
        """Load a payment intent by gateway transaction number FOR UPDATE."""
        result: PaymentIntent | None = await self._session.scalar(
            select(PaymentIntent)
            .where(PaymentIntent.paylink_transaction_no == transaction_no)
            .with_for_update()
        )
        return result

    async def get_open_intent_for_invoice(self, invoice_id: uuid.UUID) -> PaymentIntent | None:
        """Return a still-NEW gateway intent for an invoice, or None (avoids duplicates)."""
        result: PaymentIntent | None = await self._session.scalar(
            select(PaymentIntent).where(
                PaymentIntent.reference_invoice_id == invoice_id,
                PaymentIntent.status == PaymentIntentStatus.NEW,
            )
        )
        return result

    async def mark_paid(self, intent: PaymentIntent, *, paid_at: datetime) -> None:
        """Transition an intent to PAID, stamping the settlement time."""
        intent.status = PaymentIntentStatus.PAID
        intent.paid_at = paid_at
        await self._session.flush()

    async def mark_failed(self, intent: PaymentIntent, *, reason: str) -> None:
        """Transition an intent to FAILED, recording the reason."""
        intent.status = PaymentIntentStatus.FAILED
        intent.failure_reason = reason[:255]
        await self._session.flush()
