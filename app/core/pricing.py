"""The pricing engine — the single source of pricing truth (SPEC SECTION 11).

``calculate_invoice_totals`` is a PURE function: values in, values out, no DB, no
I/O, no clock. Every caller — invoice creation, the customer preview, the admin
view, the receipt email — routes through it, so there is exactly one place in the
codebase that knows how a price is built. Reads never recompute; a later VAT or
service-fee change must not silently restate a historical invoice.

All arithmetic is Decimal, quantized ROUND_HALF_UP at exactly the named steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from app.core.money import ZERO, quantize_money


class PricingIntegrityError(Exception):
    """A computed price failed an internal invariant and must never be returned.

    Raised instead of returning a malformed price so a bug surfaces loudly at the
    boundary rather than shipping a wrong invoice that the DB CHECKs would reject.
    """


class PromoDiscountKind(StrEnum):
    """How a promo's discount is expressed."""

    PERCENT = "PERCENT"
    FIXED = "FIXED"


@dataclass(frozen=True)
class PricingItem:
    """One line the courier entered, before any platform computation.

    Attributes:
        title: Customer-facing line label.
        unit_price_amount: Net-of-tax price for a single unit (> 0).
        quantity: Units of this line (1–999).
        tax_rate: Tax fraction for this line (0.0000–1.0000, e.g. 0.1500).
        description: Optional line detail.
        position: 1-based render/tie-break order.
    """

    title: str
    unit_price_amount: Decimal
    quantity: int
    tax_rate: Decimal
    description: str | None = None
    position: int = 0


@dataclass(frozen=True)
class PricingPromo:
    """The promo values needed to compute a discount, decoupled from the ORM."""

    discount_type: PromoDiscountKind
    percent_value: Decimal | None = None
    fixed_amount: Decimal | None = None
    max_discount_amount: Decimal | None = None
    min_order_amount: Decimal = ZERO


@dataclass(frozen=True)
class PricingConfig:
    """The business-rule knobs the engine reads, sourced from settings by callers."""

    service_fee_rate: Decimal
    service_fee_min_amount: Decimal
    service_fee_max_amount: Decimal
    default_vat_rate: Decimal
    max_invoice_amount: Decimal


@dataclass(frozen=True)
class PricingLine:
    """A fully computed invoice line, ready to persist to ``invoice_items``."""

    position: int
    title: str
    description: str | None
    unit_price_amount: Decimal
    quantity: int
    tax_rate: Decimal
    line_net_amount: Decimal
    line_discount_amount: Decimal
    line_taxable_amount: Decimal
    line_tax_amount: Decimal
    line_total_amount: Decimal


@dataclass(frozen=True)
class PricingResult:
    """Every computed leg of an invoice, plus an immutable audit breakdown."""

    lines: list[PricingLine]
    items_net_amount: Decimal
    courier_fee_amount: Decimal
    courier_fee_discount_amount: Decimal
    courier_fee_tax_amount: Decimal
    service_fee_amount: Decimal
    service_fee_tax_amount: Decimal
    discount_amount: Decimal
    net_after_discount_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    breakdown: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Settlement:
    """How a paid invoice's escrow total splits on completion (SPEC SECTION 20.G, ADR 0005).

    Attributes:
        commission_amount: The platform commission on the pre-discount base.
        courier_payout_amount: What the courier receives — the PRE-discount base
            (``items_net + courier_fee``) minus commission, so a promo never underpays
            the courier.
        tax_amount: The VAT collected on the invoice, owed to SYSTEM_TAX_PAYABLE.
        platform_revenue_amount: ``total - tax - courier_payout``; equivalently
            ``service_fee + commission - promo_subsidy``. Signed — it can go negative when
            subsidies exceed fees, which is why SYSTEM_REVENUE is exempt from the
            non-negative balance CHECK.
    """

    commission_amount: Decimal
    courier_payout_amount: Decimal
    tax_amount: Decimal
    platform_revenue_amount: Decimal


def compute_settlement(
    *,
    items_net_amount: Decimal,
    courier_fee_amount: Decimal,
    tax_amount: Decimal,
    total_amount: Decimal,
    commission_rate: Decimal,
) -> Settlement:
    """Split a paid invoice's total into courier payout, tax, and platform revenue.

    The courier is paid on the PRE-discount base (ADR 0005): the platform funds any promo,
    so marketing never silently reduces the courier's pay. ``platform_revenue_amount`` is
    the residual (``total - tax - courier_payout``), so the legs always reconstruct the
    total; it absorbs the promo subsidy and can be negative.
    """
    base = quantize_money(items_net_amount + courier_fee_amount)
    commission = quantize_money(base * commission_rate)
    courier_payout = quantize_money(base - commission)
    tax = quantize_money(tax_amount)
    total = quantize_money(total_amount)
    revenue = quantize_money(total - tax - courier_payout)
    return Settlement(
        commission_amount=commission,
        courier_payout_amount=courier_payout,
        tax_amount=tax,
        platform_revenue_amount=revenue,
    )


def _compute_service_fee(base: Decimal, cfg: PricingConfig) -> Decimal:
    """Compute the clamped platform service fee on the items+courier base.

    Returns ZERO when the base is zero: the min-floor must never fire on an empty
    invoice, or the platform would charge a fee on nothing.
    """
    if base <= ZERO:
        return ZERO
    raw = quantize_money(base * cfg.service_fee_rate)
    return min(max(raw, cfg.service_fee_min_amount), cfg.service_fee_max_amount)


def _compute_discount(discountable: Decimal, promo: PricingPromo | None) -> Decimal:
    """Compute the raw discount against the discountable base (0 if no promo).

    The service fee is deliberately NOT in ``discountable`` (SPEC SECTION 11.5): the
    promo subsidises goods and craft, not the platform's own fee.
    """
    if promo is None:
        return ZERO
    if promo.discount_type is PromoDiscountKind.PERCENT:
        if promo.percent_value is None:
            raise PricingIntegrityError("A percentage promo needs percent_value.")
        d = quantize_money(discountable * promo.percent_value / Decimal(100))
        if promo.max_discount_amount is not None:
            d = min(d, promo.max_discount_amount)
    else:
        if promo.fixed_amount is None:
            raise PricingIntegrityError("A fixed promo needs fixed_amount.")
        d = promo.fixed_amount
    # A discount can never exceed the base it is applied to.
    return min(d, discountable)


def compute_promo_discount(discountable: Decimal, promo: PricingPromo) -> Decimal:
    """Compute the promo discount for a given discountable base (public wrapper).

    The promo engine uses this to preview and reserve a discount without running the
    full pricing pipeline; the invoice pipeline uses the same logic internally so the
    two never disagree.
    """
    return _compute_discount(discountable, promo)


def _allocate_discount(
    line_nets: list[Decimal],
    courier_fee_net: Decimal,
    discount: Decimal,
    discountable: Decimal,
) -> tuple[list[Decimal], Decimal]:
    """Allocate the discount pro-rata across each line and the courier fee.

    Applies the largest-remainder correction so allocations sum to ``discount``
    EXACTLY — the last component (courier fee if present, else the highest-position
    item) absorbs the rounding residue. Without this the invoice total would not
    equal the sum of its parts and the DB CHECK would reject the write.

    Returns:
        (per-line allocations, courier-fee allocation).
    """
    if discount <= ZERO or discountable <= ZERO:
        return [ZERO for _ in line_nets], ZERO

    line_alloc = [quantize_money(discount * net / discountable) for net in line_nets]
    courier_alloc = (
        quantize_money(discount * courier_fee_net / discountable)
        if courier_fee_net > ZERO
        else ZERO
    )
    residue = discount - (sum(line_alloc, ZERO) + courier_alloc)
    if residue != ZERO:
        if courier_fee_net > ZERO:
            courier_alloc += residue
        else:
            # Highest-position item with a non-zero base absorbs the residue.
            last = max((i for i, net in enumerate(line_nets) if net > ZERO), default=0)
            line_alloc[last] += residue
    return line_alloc, courier_alloc


def calculate_invoice_totals(
    items: list[PricingItem],
    courier_fee_amount: Decimal,
    promo: PricingPromo | None,
    cfg: PricingConfig,
) -> PricingResult:
    """Compute every priced leg of an invoice from raw courier inputs.

    Implements SPEC SECTION 11 verbatim, in the mandated order. The output is the
    authority persisted to ``invoices`` and ``invoice_items``; the DB CHECKs then
    re-verify the arithmetic independently.

    Args:
        items: The courier's line items (net of tax).
        courier_fee_amount: The courier's craft/labour charge, net of tax (>= 0).
        promo: The validated promo to apply, or None.
        cfg: Business-rule rates and bounds from settings.

    Returns:
        A :class:`PricingResult` carrying every leg and an audit ``breakdown``.

    Raises:
        PricingIntegrityError: An internal invariant failed; no price is returned.
    """
    # 1-2. Line nets and items net.
    line_nets = [quantize_money(i.unit_price_amount * i.quantity) for i in items]
    items_net = sum(line_nets, ZERO)

    # 3. Courier fee (already net).
    courier_fee_net = quantize_money(courier_fee_amount)

    # 4. Service fee, platform-computed and clamped.
    service_fee = _compute_service_fee(items_net + courier_fee_net, cfg)

    # 5-6. Discountable base (service fee excluded) and discount.
    discountable = items_net + courier_fee_net
    discount = _compute_discount(discountable, promo)

    # 7. Pro-rata allocation with largest-remainder correction.
    line_alloc, courier_alloc = _allocate_discount(
        line_nets, courier_fee_net, discount, discountable
    )

    # 8. Tax per component on the discounted base.
    lines: list[PricingLine] = []
    tax_amount = ZERO
    for idx, item in enumerate(items):
        taxable = line_nets[idx] - line_alloc[idx]
        line_tax = quantize_money(taxable * item.tax_rate)
        line_total = taxable + line_tax
        tax_amount += line_tax
        lines.append(
            PricingLine(
                position=item.position or idx + 1,
                title=item.title,
                description=item.description,
                unit_price_amount=quantize_money(item.unit_price_amount),
                quantity=item.quantity,
                tax_rate=item.tax_rate,
                line_net_amount=line_nets[idx],
                line_discount_amount=line_alloc[idx],
                line_taxable_amount=taxable,
                line_tax_amount=line_tax,
                line_total_amount=line_total,
            )
        )

    courier_taxable = courier_fee_net - courier_alloc
    courier_tax = quantize_money(courier_taxable * cfg.default_vat_rate)
    service_tax = quantize_money(service_fee * cfg.default_vat_rate)
    tax_amount += courier_tax + service_tax

    # 9. Totals.
    net_after_discount = items_net + courier_fee_net + service_fee - discount
    total_amount = net_after_discount + tax_amount

    # 10. Assertions — raise, never return a bad price.
    total_alloc = sum(line_alloc, ZERO) + courier_alloc
    if total_alloc != discount:
        raise PricingIntegrityError("Discount allocation does not sum to the discount.")
    reconstructed = (
        sum((line.line_total_amount for line in lines), ZERO)
        + (courier_taxable + courier_tax)
        + (service_fee + service_tax)
    )
    if reconstructed != total_amount:
        raise PricingIntegrityError("Total does not equal the sum of its legs.")
    if total_amount <= ZERO:
        raise PricingIntegrityError("Invoice total must be positive.")
    if total_amount > cfg.max_invoice_amount:
        raise PricingIntegrityError("Invoice total exceeds the maximum permitted.")

    breakdown: dict[str, object] = {
        "items_net_amount": str(items_net),
        "courier_fee_amount": str(courier_fee_net),
        "service_fee_amount": str(service_fee),
        "discount_amount": str(discount),
        "net_after_discount_amount": str(net_after_discount),
        "tax_amount": str(tax_amount),
        "total_amount": str(total_amount),
        "lines": [
            {
                "position": line.position,
                "line_net_amount": str(line.line_net_amount),
                "line_discount_amount": str(line.line_discount_amount),
                "line_taxable_amount": str(line.line_taxable_amount),
                "line_tax_amount": str(line.line_tax_amount),
                "line_total_amount": str(line.line_total_amount),
            }
            for line in lines
        ],
        "courier_fee_tax_amount": str(courier_tax),
        "service_fee_tax_amount": str(service_tax),
    }

    return PricingResult(
        lines=lines,
        items_net_amount=items_net,
        courier_fee_amount=courier_fee_net,
        courier_fee_discount_amount=courier_alloc,
        courier_fee_tax_amount=courier_tax,
        service_fee_amount=service_fee,
        service_fee_tax_amount=service_tax,
        discount_amount=discount,
        net_after_discount_amount=net_after_discount,
        tax_amount=tax_amount,
        total_amount=total_amount,
        breakdown=breakdown,
    )
