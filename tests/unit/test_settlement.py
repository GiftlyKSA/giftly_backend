"""Tests for the settlement split (SPEC SECTION 20.G, ADR 0005).

The courier is paid on the PRE-discount base minus commission; the platform funds any
promo, so revenue (not the courier) absorbs the subsidy.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.pricing import compute_settlement

_RATE = Decimal("0.10")


def test_settlement_golden_with_promo() -> None:
    # The 655.50 promo invoice: items 500 + courier 100, tax 85.50.
    s = compute_settlement(
        items_net_amount=Decimal("500.00"),
        courier_fee_amount=Decimal("100.00"),
        tax_amount=Decimal("85.50"),
        total_amount=Decimal("655.50"),
        commission_rate=_RATE,
    )
    assert s.commission_amount == Decimal("60.00")
    assert s.courier_payout_amount == Decimal("540.00")
    assert s.tax_amount == Decimal("85.50")
    assert s.platform_revenue_amount == Decimal("30.00")
    # The legs reconstruct the total.
    assert s.courier_payout_amount + s.tax_amount + s.platform_revenue_amount == Decimal("655.50")


def test_settlement_no_promo() -> None:
    # The 724.50 invoice (no discount): same base, tax 94.50.
    s = compute_settlement(
        items_net_amount=Decimal("500.00"),
        courier_fee_amount=Decimal("100.00"),
        tax_amount=Decimal("94.50"),
        total_amount=Decimal("724.50"),
        commission_rate=_RATE,
    )
    assert s.courier_payout_amount == Decimal("540.00")
    assert s.platform_revenue_amount == Decimal("90.00")  # service_fee 30 + commission 60


def test_settlement_revenue_absorbs_subsidy_and_can_go_negative() -> None:
    # A generous promo (big discount, low fees) drives platform revenue negative — the
    # platform funds the promo, the courier is still paid on the pre-discount base.
    s = compute_settlement(
        items_net_amount=Decimal("500.00"),
        courier_fee_amount=Decimal("100.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("300.00"),  # customer paid far less than the 600 base
        commission_rate=_RATE,
    )
    assert s.courier_payout_amount == Decimal("540.00")
    assert s.platform_revenue_amount == Decimal("-240.00")
