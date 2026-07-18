"""Promo persistence used by the admin dashboard (SPEC SECTION 12, 18.3).

Codes are always normalized (``strip().upper()``) before storage or lookup so a stray
lowercase row cannot create a second, unreachable code that doubles a usage cap.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Promo, PromoRedemption
from app.models.enums import PromoDiscountType, PromoRedemptionStatus


def normalize_code(code: str) -> str:
    """Normalize a promo code to its canonical stored form."""
    return code.strip().upper()


class PromoRepository:
    """Creates, reads, and toggles promos, and lists their redemptions."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def get(self, promo_id: uuid.UUID) -> Promo | None:
        """Return a promo by id, or None."""
        return await self._session.get(Promo, promo_id)

    async def get_by_code(self, code: str) -> Promo | None:
        """Return a promo by its normalized code, or None."""
        result: Promo | None = await self._session.scalar(
            select(Promo).where(Promo.code == normalize_code(code))
        )
        return result

    async def list_all(self, limit: int = 100) -> list[Promo]:
        """Return promos, newest first."""
        query = select(Promo).order_by(Promo.created_at.desc()).limit(limit)
        return list(await self._session.scalars(query))

    async def create(
        self,
        *,
        code: str,
        description: str,
        discount_type: PromoDiscountType,
        percent_value: Decimal | None,
        fixed_amount: Decimal | None,
        max_discount_amount: Decimal | None,
        min_order_amount: Decimal,
        max_total_usages: int | None,
        max_usages_per_user: int,
        created_by_admin_id: uuid.UUID,
    ) -> Promo:
        """Insert a new promo with a normalized code."""
        promo = Promo(
            code=normalize_code(code),
            description=description,
            discount_type=discount_type,
            percent_value=percent_value,
            fixed_amount=fixed_amount,
            max_discount_amount=max_discount_amount,
            min_order_amount=min_order_amount,
            max_total_usages=max_total_usages,
            max_usages_per_user=max_usages_per_user,
            created_by_admin_id=created_by_admin_id,
        )
        self._session.add(promo)
        await self._session.flush()
        return promo

    async def set_active(self, promo: Promo, *, is_active: bool) -> None:
        """Activate or deactivate a promo."""
        promo.is_active = is_active
        await self._session.flush()

    async def atomic_reserve(self, promo_id: uuid.UUID) -> int | None:
        """Atomically claim one usage slot (SPEC SECTION 12.3).

        A single conditional UPDATE increments ``used_count`` only if the promo is
        active, in its window, and under its global cap — this is the whole point of
        "first 20": a SELECT-then-UPDATE would let concurrent requests all see the same
        count and overshoot the cap. Returns the new ``used_count``, or None if the
        promo is exhausted/invalid.
        """
        result = await self._session.execute(
            text(
                """
                UPDATE promos
                   SET used_count = used_count + 1
                 WHERE id = :promo_id
                   AND is_active = TRUE
                   AND (starts_at IS NULL OR starts_at <= now())
                   AND (ends_at IS NULL OR ends_at > now())
                   AND (max_total_usages IS NULL OR used_count < max_total_usages)
                RETURNING used_count
                """
            ),
            {"promo_id": promo_id},
        )
        row = result.first()
        return int(row[0]) if row is not None else None

    async def atomic_release(self, promo_id: uuid.UUID) -> None:
        """Atomically return one usage slot to the pool (SPEC SECTION 12.4)."""
        await self._session.execute(
            text(
                "UPDATE promos SET used_count = used_count - 1 "
                "WHERE id = :promo_id AND used_count > 0"
            ),
            {"promo_id": promo_id},
        )

    async def count_user_redemptions(self, promo_id: uuid.UUID, user_id: uuid.UUID) -> int:
        """Count this user's non-RELEASED redemptions of a promo (per-user cap check)."""
        total = await self._session.scalar(
            select(func.count())
            .select_from(PromoRedemption)
            .where(
                PromoRedemption.promo_id == promo_id,
                PromoRedemption.user_id == user_id,
                PromoRedemption.status != PromoRedemptionStatus.RELEASED,
            )
        )
        return int(total or 0)

    async def insert_redemption(
        self,
        *,
        promo_id: uuid.UUID,
        user_id: uuid.UUID,
        invoice_id: uuid.UUID,
        order_id: uuid.UUID,
        discount_amount: Decimal,
    ) -> PromoRedemption:
        """Insert a RESERVED redemption row."""
        redemption = PromoRedemption(
            promo_id=promo_id,
            user_id=user_id,
            invoice_id=invoice_id,
            order_id=order_id,
            discount_amount=discount_amount,
            status=PromoRedemptionStatus.RESERVED,
        )
        self._session.add(redemption)
        await self._session.flush()
        return redemption

    async def get_redemption_by_invoice(self, invoice_id: uuid.UUID) -> PromoRedemption | None:
        """Return the redemption for an invoice, or None."""
        result: PromoRedemption | None = await self._session.scalar(
            select(PromoRedemption).where(PromoRedemption.invoice_id == invoice_id)
        )
        return result

    async def set_redemption_status(
        self, redemption: PromoRedemption, status: PromoRedemptionStatus, when: datetime
    ) -> None:
        """Transition a redemption to CONSUMED or RELEASED, stamping the timestamp."""
        redemption.status = status
        if status is PromoRedemptionStatus.CONSUMED:
            redemption.consumed_at = when
        elif status is PromoRedemptionStatus.RELEASED:
            redemption.released_at = when
        await self._session.flush()

    async def list_redemptions(
        self, promo_id: uuid.UUID, limit: int = 100
    ) -> list[PromoRedemption]:
        """Return a promo's redemptions, newest first."""
        query = (
            select(PromoRedemption)
            .where(PromoRedemption.promo_id == promo_id)
            .order_by(PromoRedemption.created_at.desc())
            .limit(limit)
        )
        return list(await self._session.scalars(query))
