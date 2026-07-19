"""Pydantic contracts for delivery, approval, disputes, and ratings (SPEC SECTION 20)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.money import MoneyError, parse_money


class DeliverRequest(BaseModel):
    """A courier's geofenced delivery submission."""

    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(..., ge=-90, le=90, description="Courier's current latitude.")
    longitude: float = Field(..., ge=-180, le=180, description="Courier's current longitude.")
    proof_media_keys: list[str] = Field(
        ..., min_length=1, max_length=5, description="Confirmed delivery-proof photo keys."
    )
    note: Annotated[str, StringConstraints(max_length=500)] | None = None


class DisputeRequest(BaseModel):
    """Open a dispute on an order."""

    model_config = ConfigDict(extra="forbid")
    reason: Annotated[str, StringConstraints(min_length=3, max_length=1000)]


class ResolveDisputeRequest(BaseModel):
    """Admin resolution of a dispute."""

    model_config = ConfigDict(extra="forbid")
    outcome: str = Field(..., description="RESOLVED_CUSTOMER | RESOLVED_COURIER | RESOLVED_SPLIT.")
    note: Annotated[str, StringConstraints(max_length=1000)] | None = None
    courier_amount: Annotated[str, StringConstraints(max_length=20)] | None = Field(
        None, description="Courier's share for a SPLIT resolution (decimal string)."
    )

    @field_validator("courier_amount")
    @classmethod
    def _valid_amount(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            if parse_money(value) < Decimal(0):
                raise ValueError("courier_amount must not be negative.")
        except MoneyError as exc:
            raise ValueError(str(exc)) from exc
        return value


class DisputeResponse(BaseModel):
    """A dispute's state."""

    id: str
    order_id: str
    status: str
    reason: str
    resolution_note: str | None = None


class RatingRequest(BaseModel):
    """Rate the other party on a completed order."""

    model_config = ConfigDict(extra="forbid")
    score: int = Field(..., ge=1, le=5)
    comment: Annotated[str, StringConstraints(max_length=500)] | None = None


class RatingResponse(BaseModel):
    """A created rating."""

    id: str
    order_id: str
    rated_user_id: str
    score: int
    comment: str | None = None


class RatingSummaryResponse(BaseModel):
    """A user's aggregate received rating."""

    user_id: str
    average_score: str
    count: int
