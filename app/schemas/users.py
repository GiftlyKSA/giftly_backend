"""Pydantic contracts for the users endpoints (SPEC SECTION 19)."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class UserMeResponse(BaseModel):
    """The authenticated user's own profile."""

    id: str = Field(..., description="User id.")
    phone: str = Field(..., description="E.164 phone.")
    role: str = Field(..., description="User role.")
    status: str = Field(..., description="Account status.")
    full_name: str | None = Field(None, description="Display name.")
    email: str | None = Field(None, description="Email, used only for the paid receipt.")
    rating: str = Field(..., description="Denormalized average rating as a decimal string.")
    rating_count: int = Field(..., description="Number of ratings received.")


class UserUpdateRequest(BaseModel):
    """Editable profile fields."""

    model_config = ConfigDict(extra="forbid")
    full_name: Annotated[str, StringConstraints(max_length=120)] | None = None
    email: (
        Annotated[str, StringConstraints(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
        | None
    ) = None
    dob: date | None = None
