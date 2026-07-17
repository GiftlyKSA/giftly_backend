# Flow: invoice & pricing

The courier authors an itemised invoice; the **platform** computes the service fee, the
discount allocation, and all tax. The client never sends the service fee, discount, tax,
or total — those are server-computed from the one pricing engine (`app/core/pricing.py`).

## Algorithm (exact order)

1. `line_net[i] = round(unit_price[i] * quantity[i])`
2. `items_net = Σ line_net[i]`
3. `courier_fee_net` = the courier's input (already net, ≥ 0)
4. `service_fee = clamp(round((items_net + courier_fee_net) * SERVICE_FEE_RATE),
   min, max)` — and `0` if the base is `0`
5. `discountable = items_net + courier_fee_net` (the service fee is **not** discounted)
6. discount: PERCENT `round(discountable * pct/100)` capped by `max_discount_amount`;
   FIXED `fixed_amount`; then `min(discount, discountable)`
7. allocate the discount pro-rata across each line and the courier fee, with a
   largest-remainder correction so the allocations sum to the discount **exactly**
8. tax per component on the **discounted** base; `tax_amount = Σ line_tax + courier_tax +
   service_tax`
9. `net_after_discount = items_net + courier_fee_net + service_fee - discount`;
   `total = net_after_discount + tax_amount`

All money is `Decimal`, rounded `ROUND_HALF_UP` at exactly those steps.

## Worked example (the golden test)

`SERVICE_FEE_RATE=0.05`, `DEFAULT_VAT_RATE=0.15`, promo `WELCOME10` = 10% capped at
100.00.

Items:

| # | title | unit | qty | tax | line_net |
| --- | --- | --- | --- | --- | --- |
| 1 | Hand-painted ceramic vase | 400.00 | 1 | 0.1500 | 400.00 |
| 2 | Gift wrapping, silk | 50.00 | 2 | 0.1500 | 100.00 |

- `items_net = 500.00`, `courier_fee_net = 100.00`
- `service_fee = round(600.00 * 0.05) = 30.00`
- `discountable = 600.00`, `discount = round(600.00 * 0.10) = 60.00` (≤ cap 100.00)
- allocation: item1 `400/600 → 40.00`, item2 `100/600 → 10.00`, courier `→ 10.00`
  (Σ = 60.00 exactly)
- taxes: item1 `360.00 * .15 = 54.00`, item2 `90.00 * .15 = 13.50`,
  courier `90.00 * .15 = 13.50`, service `30.00 * .15 = 4.50` → `tax = 85.50`
- `net_after_discount = 500 + 100 + 30 − 60 = 570.00`
- **`total = 570.00 + 85.50 = 655.50`**

## Customer-facing breakdown (render in exactly this order)

```
Items (net)                500.00
Courier fee                100.00
Service fee                 30.00
Discount (WELCOME10)       -60.00
---------------------------------
Subtotal                   570.00
VAT                         85.50
=================================
Total                      655.50
Paid from wallet           300.00
Paid by card/Apple Pay     355.50
```

Every stored amount on the invoice and its items is the output of this engine; reads
never recompute, so a later VAT or service-fee change never restates a historical
invoice.
