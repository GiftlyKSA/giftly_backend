"""Ratings on completed orders (SPEC SECTION 20.I).

A participant rates the OTHER party once per order, and only after the order is
COMPLETED. The rated user is derived from the order — never taken from the request — so
a rater can neither rate themselves nor a stranger.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.core.exceptions import ConflictError, NotFoundError
from app.models import Rating
from app.models.enums import OrderStatus
from app.repositories.order_repository import OrderRepository
from app.repositories.rating_repository import RatingRepository


class RatingService:
    """Creates ratings and reports a user's average score."""

    def __init__(self, *, orders: OrderRepository, ratings: RatingRepository) -> None:
        """Wire the order and rating repositories."""
        self._orders = orders
        self._ratings = ratings

    async def rate(
        self,
        *,
        order_id: uuid.UUID,
        rater_id: uuid.UUID,
        score: int,
        comment: str | None,
    ) -> Rating:
        """Rate the other participant of a completed order.

        Raises:
            NotFoundError: Not a participant's order.
            ConflictError: The order is not completed, or the rater already rated it.
        """
        order = await self._orders.get_for_actor(order_id, rater_id)
        if order is None:
            raise NotFoundError("Order not found.")
        if order.status is not OrderStatus.COMPLETED:
            raise ConflictError("You can only rate a completed order.")

        rated_user_id = order.courier_id if rater_id == order.customer_id else order.customer_id
        if rated_user_id is None:  # pragma: no cover - a completed order always has a courier
            raise ConflictError("This order has no counterparty to rate.")
        if await self._ratings.exists_for_rater(order_id, rater_id):
            raise ConflictError("You have already rated this order.")

        return await self._ratings.create(
            order_id=order_id,
            rater_id=rater_id,
            rated_user_id=rated_user_id,
            score=score,
            comment=comment,
        )

    async def summary_for_user(self, user_id: uuid.UUID) -> tuple[Decimal, int]:
        """Return a user's (average score, number of ratings received)."""
        return await self._ratings.summary_for_user(user_id)
