"""Pydantic contracts for the auth endpoints (SPEC SECTION 19).

Every inbound model forbids extra fields (mass assignment is an attack) and validates
strict types and bounds at the boundary. Phone numbers are Saudi E.164 mobiles.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_Phone = Annotated[str, StringConstraints(pattern=r"^\+9665\d{8}$")]
_Otp = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class SendOtpRequest(BaseModel):
    """Request an OTP for a phone number."""

    model_config = ConfigDict(extra="forbid")
    phone: _Phone = Field(..., description="Saudi E.164 mobile.", examples=["+966501234567"])


class SendOtpResponse(BaseModel):
    """OTP request accepted."""

    expires_in: int = Field(..., description="Seconds until the OTP expires.", examples=[180])
    dev_otp: str | None = Field(
        None, description="The OTP, returned only in development to ease local testing."
    )


class VerifyOtpRequest(BaseModel):
    """Verify a submitted OTP."""

    model_config = ConfigDict(extra="forbid")
    phone: _Phone = Field(..., description="Saudi E.164 mobile.")
    otp: _Otp = Field(..., description="The 6-digit code.", examples=["849201"])


class VerifyOtpResponse(BaseModel):
    """The result of verifying an OTP: tokens for an existing user, else a handoff."""

    is_new_user: bool = Field(..., description="True when no account exists for the phone.")
    role: str | None = Field(None, description="The user's role, when tokens are issued.")
    access_token: str | None = Field(None, description="30-minute access JWT, if existing user.")
    refresh_token: str | None = Field(None, description="30-day refresh token, if existing user.")
    registration_token: str | None = Field(
        None, description="Short-lived token to call /api/auth/register, if a new user."
    )


class RegisterRequest(BaseModel):
    """Create an account authorised by a registration token."""

    model_config = ConfigDict(extra="forbid")
    registration_token: str = Field(..., description="From verify-otp when is_new_user is true.")
    role: Literal["CUSTOMER", "COURIER"] = Field(..., description="The account role to create.")
    full_name: Annotated[str, StringConstraints(max_length=120)] | None = None
    email: (
        Annotated[str, StringConstraints(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
        | None
    ) = None
    dob: date | None = None
    city: Annotated[str, StringConstraints(max_length=100)] | None = Field(
        None, description="Required for couriers."
    )
    national_id: Annotated[str, StringConstraints(max_length=64)] | None = Field(
        None, description="Courier identity (national id or passport required)."
    )
    passport_id: Annotated[str, StringConstraints(max_length=64)] | None = None


class RefreshRequest(BaseModel):
    """Exchange a refresh token for a new token pair."""

    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(..., description="The current refresh token.")


class TokenResponse(BaseModel):
    """An issued access + refresh token pair."""

    access_token: str = Field(..., description="30-minute access JWT.")
    refresh_token: str = Field(..., description="30-day rotating refresh token.")
    role: str = Field(..., description="The authenticated user's role.")
