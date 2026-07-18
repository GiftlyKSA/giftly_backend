"""Pydantic contracts for the payment endpoints (SPEC SECTION 5.1, 19).

Money crosses the wire as decimal strings, never floats. Amounts the client sends
(a top-up value) are parsed to Decimal; the gateway webhook payload is validated the
same way before the raw-body signature is trusted.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.money import MoneyError, parse_money

_MoneyStr = Annotated[str, StringConstraints(min_length=1, max_length=20)]
_Txn = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class TopupRequest(BaseModel):
    """Start a wallet top-up for a bounded amount."""

    model_config = ConfigDict(extra="forbid")
    amount: _MoneyStr = Field(..., description='Top-up amount, e.g. "500.00".')

    @field_validator("amount")
    @classmethod
    def _valid_amount(cls, value: str) -> str:
        try:
            if parse_money(value) <= Decimal(0):
                raise ValueError("amount must be positive.")
        except MoneyError as exc:
            raise ValueError(str(exc)) from exc
        return value


class TopupResponse(BaseModel):
    """A started top-up: redirect the client to ``payment_url``."""

    payment_intent_id: str
    amount: str
    payment_url: str


class PayInvoiceResponse(BaseModel):
    """The result of paying an invoice.

    ``status`` is ``PAID`` when the wallet covered the total (settled immediately) or
    ``PENDING`` when a gateway payment is required — then ``payment_url`` is set.
    """

    invoice_id: str
    status: str
    amount_from_wallet: str
    amount_from_gateway: str
    payment_url: str | None = None


class PaylinkWebhookPayload(BaseModel):
    """The gateway webhook body (also validated from the RAW bytes in the service)."""

    model_config = ConfigDict(extra="ignore")
    transaction_no: _Txn
    status: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    amount: _MoneyStr


class WebhookAck(BaseModel):
    """The webhook acknowledgement returned to the gateway."""

    outcome: str


class SimulatePaymentRequest(BaseModel):
    """Development-only: simulate a gateway callback for a transaction number."""

    model_config = ConfigDict(extra="forbid")
    transaction_no: _Txn
    status: Annotated[str, StringConstraints(min_length=1, max_length=32)] = "PAID"
