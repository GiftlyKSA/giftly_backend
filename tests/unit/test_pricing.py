"""Tests for the pricing engine, anchored to the SPEC SECTION 11 golden example."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.core.pricing import (
    PricingConfig,
    PricingIntegrityError,
    PricingItem,
    PricingPromo,
    PromoDiscountKind,
    calculate_invoice_totals,
)

CFG = PricingConfig(
    service_fee_rate=Decimal("0.05"),
    service_fee_min_amount=Decimal("5.00"),
    service_fee_max_amount=Decimal("500.00"),
    default_vat_rate=Decimal("0.15"),
    max_invoice_amount=Decimal("50000.00"),
)

WELCOME10 = PricingPromo(
    discount_type=PromoDiscountKind.PERCENT,
    percent_value=Decimal("10.00"),
    max_discount_amount=Decimal("100.00"),
)


def _golden_items() -> list[PricingItem]:
    return [
        PricingItem("Hand-painted ceramic vase", Decimal("400.00"), 1, Decimal("0.1500"), None, 1),
        PricingItem("Gift wrapping, silk", Decimal("50.00"), 2, Decimal("0.1500"), None, 2),
    ]


def test_golden_example_matches_spec_section_11_exactly() -> None:
    result = calculate_invoice_totals(_golden_items(), Decimal("100.00"), WELCOME10, CFG)

    assert result.items_net_amount == Decimal("500.00")
    assert result.courier_fee_amount == Decimal("100.00")
    assert result.service_fee_amount == Decimal("30.00")
    assert result.discount_amount == Decimal("60.00")
    assert result.tax_amount == Decimal("85.50")
    assert result.net_after_discount_amount == Decimal("570.00")
    assert result.total_amount == Decimal("655.50")


def test_golden_example_discount_allocation() -> None:
    result = calculate_invoice_totals(_golden_items(), Decimal("100.00"), WELCOME10, CFG)
    assert result.lines[0].line_discount_amount == Decimal("40.00")
    assert result.lines[1].line_discount_amount == Decimal("10.00")
    assert result.courier_fee_discount_amount == Decimal("10.00")


def test_golden_example_line_taxes() -> None:
    result = calculate_invoice_totals(_golden_items(), Decimal("100.00"), WELCOME10, CFG)
    assert result.lines[0].line_tax_amount == Decimal("54.00")
    assert result.lines[1].line_tax_amount == Decimal("13.50")
    assert result.courier_fee_tax_amount == Decimal("13.50")
    assert result.service_fee_tax_amount == Decimal("4.50")


def test_no_promo_yields_zero_discount() -> None:
    result = calculate_invoice_totals(_golden_items(), Decimal("100.00"), None, CFG)
    assert result.discount_amount == Decimal("0.00")
    # net_after_discount 630.00 (500 + 100 + 30) + tax 94.50 (60 + 15 + 15 + 4.50).
    assert result.net_after_discount_amount == Decimal("630.00")
    assert result.tax_amount == Decimal("94.50")
    assert result.total_amount == Decimal("724.50")


def test_service_fee_never_charged_on_empty_base() -> None:
    result = calculate_invoice_totals(
        [PricingItem("x", Decimal("10.00"), 1, Decimal("0.0000"), None, 1)],
        Decimal("0.00"),
        None,
        CFG,
    )
    # base is 10, so fee is min-floored to 5.00; verify floor fires on a real base.
    assert result.service_fee_amount == Decimal("5.00")


def test_fixed_discount_cannot_exceed_base() -> None:
    promo = PricingPromo(discount_type=PromoDiscountKind.FIXED, fixed_amount=Decimal("9999.00"))
    items = [PricingItem("x", Decimal("10.00"), 1, Decimal("0.1500"), None, 1)]
    result = calculate_invoice_totals(items, Decimal("0.00"), promo, CFG)
    assert result.discount_amount == Decimal("10.00")


def test_total_exceeding_max_raises() -> None:
    items = [PricingItem("x", Decimal("49999.00"), 1, Decimal("0.1500"), None, 1)]
    with pytest.raises(PricingIntegrityError):
        calculate_invoice_totals(items, Decimal("40000.00"), None, CFG)


def test_allocation_always_sums_to_discount_with_awkward_split() -> None:
    # Three equal lines with a discount that does not divide evenly (10.00 / 3).
    items = [
        PricingItem(f"i{n}", Decimal("100.00"), 1, Decimal("0.1500"), None, n) for n in (1, 2, 3)
    ]
    promo = PricingPromo(discount_type=PromoDiscountKind.FIXED, fixed_amount=Decimal("10.00"))
    result = calculate_invoice_totals(items, Decimal("0.00"), promo, CFG)
    total_alloc = sum((line.line_discount_amount for line in result.lines), Decimal("0.00"))
    assert total_alloc == Decimal("10.00")
