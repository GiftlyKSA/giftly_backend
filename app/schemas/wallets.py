"""Pydantic contracts for the wallet endpoints (SPEC SECTION 19).

Money is always a decimal STRING, never a number.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, SecretStr, field_validator

_SAUDI_IBAN = re.compile(r"^SA\d{22}$")


class WalletResponse(BaseModel):
    """A user's wallet snapshot."""

    balance: str = Field(..., description="Total balance as a decimal string.", examples=["300.00"])
    held_balance: str = Field(..., description="Funds held (e.g. an escrow hold).")
    available: str = Field(..., description="balance - held_balance.")
    currency: str = Field(..., description="ISO currency code.", examples=["SAR"])


class TransactionResponse(BaseModel):
    """One ledger entry as seen by its wallet owner."""

    id: str = Field(..., description="Transaction id.")
    amount: str = Field(..., description="Signed amount as a decimal string.")
    type: str = Field(..., description="Transaction type.")
    status: str = Field(..., description="PENDING | SETTLED | REVERSED.")
    balance_after: str = Field(..., description="Wallet balance after this entry.")
    created_at: str = Field(..., description="ISO-8601 UTC timestamp.")


class TransactionPage(BaseModel):
    """A keyset page of transactions."""

    items: list[TransactionResponse] = Field(..., description="Newest first.")
    next_cursor: str | None = Field(None, description="Pass back to fetch the next page.")


class WithdrawalRequest(BaseModel):
    """A courier request to withdraw available wallet funds."""

    amount: str = Field(..., description="Amount as a decimal string.", examples=["250.00"])
    iban: SecretStr = Field(..., description="Saudi IBAN; encrypted immediately at rest.")

    @field_validator("iban", mode="before")
    @classmethod
    def normalize_iban(cls, value: object) -> str:
        """Normalize spaces/case and reject malformed Saudi IBANs."""
        raw = str(value).replace(" ", "").upper()
        if not _SAUDI_IBAN.fullmatch(raw):
            raise ValueError("IBAN must be a valid 24-character Saudi IBAN.")
        return raw


class WithdrawalResponse(BaseModel):
    """A masked courier withdrawal record."""

    id: str
    amount: str
    iban_last4: str
    status: str
    rejection_reason: str | None = None


class RejectWithdrawalRequest(BaseModel):
    """An admin rejection reason for a withdrawal."""

    reason: str = Field(..., min_length=1, max_length=255)
