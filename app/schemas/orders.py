"""Pydantic contracts for the order endpoints (SPEC SECTION 19)."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class CreateOrderRequest(BaseModel):
    """Create a gift-request order."""

    model_config = ConfigDict(extra="forbid")
    description: Annotated[str, StringConstraints(max_length=2000)] | None = None
    delivery_city: Annotated[str, StringConstraints(max_length=100)] = Field(...)
    latitude: float = Field(..., ge=-90, le=90, description="Drop-off latitude.")
    longitude: float = Field(..., ge=-180, le=180, description="Drop-off longitude.")
    delivery_date: date = Field(..., description="Requested delivery date (<= 6 months out).")
    request_media_keys: list[str] = Field(
        default_factory=list, max_length=3, description="Confirmed request-photo keys (0–3)."
    )


class CancelOrderRequest(BaseModel):
    """Cancel an order before it is in progress."""

    model_config = ConfigDict(extra="forbid")
    reason: Annotated[str, StringConstraints(max_length=255)] | None = None


class OrderSummary(BaseModel):
    """A compact order row for lists and the radar (no exact coordinates)."""

    id: str
    status: str
    delivery_city: str
    delivery_date: str
    description: str | None
    created_at: str


class OrderDetail(BaseModel):
    """A full order view for its participants."""

    id: str
    status: str
    customer_id: str
    courier_id: str | None
    delivery_city: str
    delivery_date: str
    description: str | None
    latitude: float | None = Field(None, description="Shown to a courier only once assigned.")
    longitude: float | None = None
    total_amount: str
    assigned_at: str | None
    created_at: str


class OrderListResponse(BaseModel):
    """A keyset page of order summaries."""

    items: list[OrderSummary]
    next_cursor: str | None = None
