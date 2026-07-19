"""Rating routes (SPEC SECTION 20.I).

A participant rates the other party once per completed order. The rated user is derived
from the order, never the request body. Anyone authenticated may read a user's aggregate.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Actor, get_db, require_auth, require_role
from app.models.enums import UserRole
from app.repositories.order_repository import OrderRepository
from app.repositories.rating_repository import RatingRepository
from app.schemas.fulfillment import RatingRequest, RatingResponse, RatingSummaryResponse
from app.services.rating_service import RatingService

router = APIRouter(prefix="/api", tags=["ratings"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
_Participant = require_role(UserRole.CUSTOMER, UserRole.COURIER)


def _service(db: AsyncSession) -> RatingService:
    return RatingService(orders=OrderRepository(db), ratings=RatingRepository(db))


@router.post("/orders/{order_id}/ratings", response_model=RatingResponse, status_code=201)
async def rate_order(
    db: DbDep,
    order_id: uuid.UUID,
    body: RatingRequest,
    actor: Annotated[Actor, Depends(_Participant)],
) -> RatingResponse:
    """Rate the other party on a completed order."""
    rating = await _service(db).rate(
        order_id=order_id, rater_id=actor.id, score=body.score, comment=body.comment
    )
    return RatingResponse(
        id=str(rating.id),
        order_id=str(rating.order_id),
        rated_user_id=str(rating.rated_user_id),
        score=rating.score,
        comment=rating.comment,
    )


@router.get("/users/{user_id}/ratings/summary", response_model=RatingSummaryResponse)
async def user_rating_summary(
    db: DbDep,
    user_id: uuid.UUID,
    _actor: Annotated[Actor, Depends(require_auth)],
) -> RatingSummaryResponse:
    """Return a user's average received score and rating count."""
    average, count = await _service(db).summary_for_user(user_id)
    return RatingSummaryResponse(user_id=str(user_id), average_score=f"{average:.2f}", count=count)
