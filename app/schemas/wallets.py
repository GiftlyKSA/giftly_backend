"""Pydantic contracts for the wallet endpoints (SPEC SECTION 19).

Money is always a decimal STRING, never a number.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
