"""Tests for the money primitives."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.core.money import MoneyError, money_str, parse_money, parse_rate, quantize_money


def test_parse_money_from_string() -> None:
    assert parse_money("655.50") == Decimal("655.50")


def test_parse_money_rejects_float() -> None:
    with pytest.raises(MoneyError):
        parse_money(0.1)  # type: ignore[arg-type]


def test_parse_money_rejects_garbage() -> None:
    with pytest.raises(MoneyError):
        parse_money("not-money")


def test_parse_money_rounds_half_up() -> None:
    assert parse_money("1.005") == Decimal("1.01")


def test_quantize_money_two_places() -> None:
    assert quantize_money(Decimal("1.1")) == Decimal("1.10")


def test_money_str_canonical() -> None:
    assert money_str(Decimal("655.5")) == "655.50"


def test_parse_rate_four_places() -> None:
    assert parse_rate("0.15") == Decimal("0.1500")


def test_parse_rate_rejects_float() -> None:
    with pytest.raises(MoneyError):
        parse_rate(0.15)  # type: ignore[arg-type]
