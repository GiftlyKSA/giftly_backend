"""Rating persistence (SPEC SECTION 20.I).

One rating per rater per order (``uq_ratings_order_rater``); a rater may never rate
themselves (``chk_no_self_rating``). Ownership of the order is checked in the service.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Rating


class RatingRepository:
    """Creates ratings and aggregates a user's received scores."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def create(
        self,
        *,
        order_id: uuid.UUID,
        rater_id: uuid.UUID,
        rated_user_id: uuid.UUID,
        score: int,
        comment: str | None,
    ) -> Rating:
        """Insert a rating (uniqueness and self-rating are enforced by DB constraints)."""
        rating = Rating(
            order_id=order_id,
            rater_id=rater_id,
            rated_user_id=rated_user_id,
            score=score,
            comment=comment,
        )
        self._session.add(rating)
        await self._session.flush()
        return rating

    async def exists_for_rater(self, order_id: uuid.UUID, rater_id: uuid.UUID) -> bool:
        """True if this rater has already rated this order."""
        found = await self._session.scalar(
            select(Rating.id).where(Rating.order_id == order_id, Rating.rater_id == rater_id)
        )
        return found is not None

    async def summary_for_user(self, user_id: uuid.UUID) -> tuple[Decimal, int]:
        """Return (average score to 2dp, count) of ratings a user has received."""
        row = (
            await self._session.execute(
                select(func.avg(Rating.score), func.count(Rating.id)).where(
                    Rating.rated_user_id == user_id
                )
            )
        ).first()
        if row is None or row[1] == 0:
            return Decimal("0.00"), 0
        return Decimal(str(row[0])).quantize(Decimal("0.01")), int(row[1])
