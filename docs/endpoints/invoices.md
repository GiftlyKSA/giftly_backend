# Invoices

The courier authors an itemised invoice; the **platform** computes the service fee, the
discount allocation, and all tax through the one pricing engine (`app/core/pricing.py`).
The client never sends the service fee, discount, tax, or total. Every stored amount is
the engine's output, **frozen once issued** — reads never recompute, so a later VAT or
service-fee change never restates a historical invoice. All money is a decimal STRING.

## POST /api/orders/{order_id}/invoices
Author and issue an invoice for an order. **Auth**: Bearer JWT · **Role**: COURIER (the
order's assigned courier). Moves the order `ASSIGNED -> WAITING_PAYMENT`.
### Body
| field | type | required | notes |
| items | array | yes | 1–20 lines |
| items[].title | string | yes | 1–120 chars |
| items[].description | string | no | <= 500 chars |
| items[].unit_price_amount | string | yes | net unit price, > 0, e.g. `"400.00"` |
| items[].quantity | integer | yes | 1–999 |
| items[].tax_rate | string | no | fraction `0`–`1`, default `"0.15"` |
| courier_fee_amount | string | no | courier's craft/labour, net, >= 0, default `"0.00"` |
| promo_code | string | no | the customer's promo; discount is computed & reserved |
### Success 201 — InvoiceResponse
Every leg is server-computed: `items_net_amount`, `service_fee_amount`, `discount_amount`,
`net_after_discount_amount`, `tax_amount`, `total_amount`, plus the priced `items[]`.
### Errors
| status | code | when |
| 404 | NOT_FOUND | not your assigned order (no existence leak) |
| 409 | CONFLICT | the order already has an active invoice |
| 409 | INVALID_STATE_TRANSITION | the order is not in an invoiceable state |
| 422 | VALIDATION_ERROR | empty/oversized items, a bad line, or a promo with no discount |
| 422 | PROMO_* | the promo failed validation (see promos.md) |

## GET /api/orders/{order_id}/invoice
The order's active invoice. **Role**: participant (CUSTOMER or COURIER). 404 to anyone else.

## GET /api/invoices/{invoice_id}
An invoice by id. **Role**: participant (CUSTOMER or COURIER). A non-participant gets 404.

## POST /api/invoices/{invoice_id}/cancel
Cancel an unpaid **ISSUED** invoice, release its promo reservation, and reopen the order
(`WAITING_PAYMENT -> ASSIGNED`) so a fresh invoice can be authored. **Role**: COURIER (the
issuing courier). The cancelled invoice row is never mutated further — a correction is a
new invoice, never an edit.
### Errors
| status | code | when |
| 404 | NOT_FOUND | no such invoice issued by you |
| 409 | INVALID_STATE_TRANSITION | the invoice is not ISSUED (already paid/cancelled) |

## The golden example (regression anchor)
Items `[vase 400.00×1 @0.15, wrapping 50.00×2 @0.15]`, courier fee `100.00`, promo `10%`
capped at `100.00`:

```
Items (net)                500.00
Courier fee                100.00
Service fee                 30.00
Discount                   -60.00
---------------------------------
Subtotal                   570.00
VAT                         85.50
=================================
Total                      655.50
```

Without the promo the same invoice totals **724.50** (VAT 94.50). See
`docs/flows/invoice-and-pricing.md` for the exact algorithm.
