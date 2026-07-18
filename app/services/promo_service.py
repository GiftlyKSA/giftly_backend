"""The promo engine — validation and atomic reserve/consume/release (SPEC SECTION 12).

Codes are case-insensitive (normalized to upper). Validation reports a precise error
per §12.2 without reserving. Reservation uses the atomic conditional UPDATE (§12.3) so
a global usage cap ("first 20") can never be overshot by concurrent requests; the
per-user cap is enforced in the same transaction with the promo row already locked by
that UPDATE. Release returns a slot to the pool so an abandoned checkout never burns
one permanently (§12.4).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.core.exceptions import (
    PromoExpiredError,
    PromoInactiveError,
    PromoMinOrderNotMetError,
    PromoNotFoundError,
    PromoNotStartedError,
    PromoUsageExceededError,
    PromoUserLimitReachedError,
)
from app.core.pricing import PricingPromo, PromoDiscountKind, compute_promo_discount
from app.models import Promo
from app.models.enums import PromoDiscountType, PromoRedemptionStatus
from app.repositories.promo_repository import PromoRepository


@dataclass(frozen=True)
class PromoValidation:
    """A validated promo and the discount it would apply to a given base."""

    promo: Promo
    discount_amount: Decimal


def _to_pricing_promo(promo: Promo) -> PricingPromo:
    """Adapt an ORM promo to the pure pricing engine's promo value object."""
    kind = (
        PromoDiscountKind.PERCENT
        if promo.discount_type is PromoDiscountType.PERCENT
        else PromoDiscountKind.FIXED
    )
    return PricingPromo(
        discount_type=kind,
        percent_value=promo.percent_value,
        fixed_amount=promo.fixed_amount,
        max_discount_amount=promo.max_discount_amount,
        min_order_amount=promo.min_order_amount,
    )


class PromoService:
    """Validates promos and manages their reservation lifecycle."""

    def __init__(self, promos: PromoRepository) -> None:
        """Bind the service to a promo repository (and its session)."""
        self._promos = promos

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    async def validate(
        self, *, code: str, discountable_base: Decimal, user_id: uuid.UUID
    ) -> PromoValidation:
        """Validate a promo against a base amount without reserving it (§12.2).

        Raises:
            PromoNotFoundError / PromoInactiveError / PromoNotStartedError /
            PromoExpiredError / PromoMinOrderNotMetError / PromoUsageExceededError /
            PromoUserLimitReachedError: The specific failure, each a 422 with its code.
        """
        promo = await self._promos.get_by_code(code)
        now = self._now()
        if promo is None:
            raise PromoNotFoundError
        if not promo.is_active:
            raise PromoInactiveError
        if promo.starts_at is not None and now < promo.starts_at:
            raise PromoNotStartedError
        if promo.ends_at is not None and now >= promo.ends_at:
            raise PromoExpiredError
        if discountable_base < promo.min_order_amount:
            raise PromoMinOrderNotMetError
        if promo.max_total_usages is not None and promo.used_count >= promo.max_total_usages:
            raise PromoUsageExceededError
        user_uses = await self._promos.count_user_redemptions(promo.id, user_id)
        if user_uses >= promo.max_usages_per_user:
            raise PromoUserLimitReachedError
        discount = compute_promo_discount(discountable_base, _to_pricing_promo(promo))
        return PromoValidation(promo=promo, discount_amount=discount)

    async def reserve(
        self,
        *,
        promo: Promo,
        user_id: uuid.UUID,
        invoice_id: uuid.UUID,
        order_id: uuid.UUID,
        discount_amount: Decimal,
    ) -> None:
        """Reserve a promo usage for an invoice (§12.3), inserting a RESERVED row.

        Must run inside the caller's transaction so a failed per-user check rolls back
        the atomic increment.

        Raises:
            PromoUsageExceededError: The global cap is exhausted (no slot claimed).
            PromoUserLimitReachedError: This user is over the per-user cap.
        """
        new_count = await self._promos.atomic_reserve(promo.id)
        if new_count is None:
            raise PromoUsageExceededError
        # The UPDATE row-locked the promo, so this count serializes against concurrent
        # reservations by the same user. Over the cap => raise => the whole tx rolls
        # back, undoing the increment above.
        user_uses = await self._promos.count_user_redemptions(promo.id, user_id)
        if user_uses >= promo.max_usages_per_user:
            raise PromoUserLimitReachedError
        await self._promos.insert_redemption(
            promo_id=promo.id,
            user_id=user_id,
            invoice_id=invoice_id,
            order_id=order_id,
            discount_amount=discount_amount,
        )

    async def consume(self, *, invoice_id: uuid.UUID) -> None:
        """Mark an invoice's reservation CONSUMED on payment (§12.4)."""
        redemption = await self._promos.get_redemption_by_invoice(invoice_id)
        if redemption is None or redemption.status is not PromoRedemptionStatus.RESERVED:
            return
        await self._promos.set_redemption_status(
            redemption, PromoRedemptionStatus.CONSUMED, self._now()
        )

    async def release(self, *, invoice_id: uuid.UUID) -> None:
        """Release an invoice's reservation, returning its slot to the pool (§12.4).

        Idempotent: releasing an already-released or consumed reservation is a no-op.
        """
        redemption = await self._promos.get_redemption_by_invoice(invoice_id)
        if redemption is None or redemption.status is not PromoRedemptionStatus.RESERVED:
            return
        await self._promos.set_redemption_status(
            redemption, PromoRedemptionStatus.RELEASED, self._now()
        )
        await self._promos.atomic_release(redemption.promo_id)
