"""Decimal money primitives for SAFE-GIFT.

This module owns every money quantization rule in the codebase (SPEC SECTION 8.9,
SECTION 11). Money is a ``Decimal`` parsed from strings and serialized to strings;
floats are banned in every money path because ``0.1 + 0.2 != 0.3`` is how halalas
silently disappear. Nothing else in the codebase may call ``.quantize`` on a money
value — it goes through :func:`quantize_money` so rounding is uniform and auditable.

This module is pure: no I/O, no clock, no DB.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# The smallest money unit (one halala) and the granularity of a tax/fee rate.
MONEY: Decimal = Decimal("0.01")
RATE: Decimal = Decimal("0.0001")
ZERO: Decimal = Decimal("0.00")


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as a well-formed money amount."""


def parse_money(value: str | Decimal | int) -> Decimal:
    """Parse an untrusted money value into a 2dp Decimal.

    Accepts strings (the wire format), Decimals, and ints. Floats are rejected on
    purpose — accepting one here is the single mistake that reintroduces binary
    rounding error into the ledger.

    Args:
        value: A decimal string like ``"655.50"``, a Decimal, or an int.

    Returns:
        The value quantized to two decimal places, ROUND_HALF_UP.

    Raises:
        MoneyError: The value is a float, not numeric, or not finite.
    """
    if isinstance(value, float):
        raise MoneyError("Money must never be a float; pass a decimal string.")
    try:
        dec = Decimal(value) if not isinstance(value, Decimal) else value
    except (InvalidOperation, TypeError) as exc:
        raise MoneyError(f"Not a valid money amount: {value!r}") from exc
    if not dec.is_finite():
        raise MoneyError("Money amount must be finite.")
    return quantize_money(dec)


def quantize_money(value: Decimal) -> Decimal:
    """Quantize a Decimal to two places using banker-free ROUND_HALF_UP.

    Every money output in the system passes through here. Centralising it means a
    rounding-policy change is a one-line change, not a codebase-wide audit.
    """
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def parse_rate(value: str | Decimal) -> Decimal:
    """Parse a tax/fee rate stored as a fraction (0.1500 == 15%) to 4dp."""
    if isinstance(value, float):
        raise MoneyError("Rate must never be a float; pass a decimal string.")
    try:
        dec = Decimal(value) if not isinstance(value, Decimal) else value
    except (InvalidOperation, TypeError) as exc:
        raise MoneyError(f"Not a valid rate: {value!r}") from exc
    if not dec.is_finite():
        raise MoneyError("Rate must be finite.")
    return dec.quantize(RATE, rounding=ROUND_HALF_UP)


def money_str(value: Decimal) -> str:
    """Serialize a money Decimal to its canonical 2dp string form."""
    return f"{quantize_money(value):.2f}"
