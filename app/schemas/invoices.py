"""Pydantic contracts for the invoice and promo-preview endpoints (SPEC SECTION 11, 14).

Money crosses the wire as decimal strings, never floats: a float is the single mistake
that reintroduces binary rounding error. Amounts the client sends (unit price, courier
fee) are parsed to Decimal; everything else on an invoice is server-computed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.money import MoneyError, parse_money, parse_rate

_Title = Annotated[str, StringConstraints(min_length=1, max_length=120)]
_Description = Annotated[str, StringConstraints(max_length=500)]
_Code = Annotated[str, StringConstraints(min_length=1, max_length=32)]
_MoneyStr = Annotated[str, StringConstraints(min_length=1, max_length=20)]
_RateStr = Annotated[str, StringConstraints(min_length=1, max_length=8)]


class InvoiceLineRequest(BaseModel):
    """One courier-authored invoice line (net of tax)."""

    model_config = ConfigDict(extra="forbid")
    title: _Title
    description: _Description | None = None
    unit_price_amount: _MoneyStr = Field(..., description='Net unit price, e.g. "400.00".')
    quantity: int = Field(..., ge=1, le=999)
    tax_rate: _RateStr = Field("0.15", description='Tax fraction, e.g. "0.15" for 15%.')

    @field_validator("unit_price_amount")
    @classmethod
    def _valid_price(cls, value: str) -> str:
        try:
            parse_money(value)
        except MoneyError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("tax_rate")
    @classmethod
    def _valid_rate(cls, value: str) -> str:
        try:
            rate = parse_rate(value)
        except MoneyError as exc:
            raise ValueError(str(exc)) from exc
        if not Decimal(0) <= rate <= Decimal(1):
            raise ValueError("tax_rate must be between 0 and 1.")
        return value


class CreateInvoiceRequest(BaseModel):
    """Author and issue an invoice for an order."""

    model_config = ConfigDict(extra="forbid")
    items: list[InvoiceLineRequest] = Field(..., min_length=1, max_length=20)
    courier_fee_amount: _MoneyStr = Field("0.00", description="Courier's craft/labour, net.")
    promo_code: _Code | None = None

    @field_validator("courier_fee_amount")
    @classmethod
    def _valid_fee(cls, value: str) -> str:
        try:
            if parse_money(value) < Decimal(0):
                raise ValueError("courier_fee_amount must not be negative.")
        except MoneyError as exc:
            raise ValueError(str(exc)) from exc
        return value


class PromoValidateRequest(BaseModel):
    """Preview a promo against an order's active invoice."""

    model_config = ConfigDict(extra="forbid")
    code: _Code
    order_id: str = Field(..., description="The order whose active invoice to price against.")


class InvoiceItemResponse(BaseModel):
    """A single computed invoice line, as stored."""

    position: int
    title: str
    description: str | None
    unit_price_amount: str
    quantity: int
    tax_rate: str
    line_net_amount: str
    line_discount_amount: str
    line_taxable_amount: str
    line_tax_amount: str
    line_total_amount: str


class InvoiceResponse(BaseModel):
    """A full invoice view for its participants (every amount server-computed)."""

    id: str
    order_id: str
    status: str
    currency: str
    items_net_amount: str
    courier_fee_amount: str
    service_fee_amount: str
    discount_amount: str
    net_after_discount_amount: str
    tax_amount: str
    total_amount: str
    promo_code: str | None
    issued_at: str | None
    expires_at: str | None
    items: list[InvoiceItemResponse]


class PromoPreviewResponse(BaseModel):
    """A previewed promo discount and the total it would produce."""

    code: str
    discount_amount: str
    original_total_amount: str
    total_amount: str
