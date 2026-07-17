"""Promo persistence used by the admin dashboard (SPEC SECTION 12, 18.3).

Codes are always normalized (``strip().upper()``) before storage or lookup so a stray
lowercase row cannot create a second, unreachable code that doubles a usage cap.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Promo, PromoRedemption
from app.models.enums import PromoDiscountType


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
