# Promos

Reserving and consuming a promo happens **inside the invoice pipeline**, never from a
client call. The only public promo endpoint is a customer-facing preview. Promo
administration (create/activate/deactivate) lives under the admin dashboard.

## POST /api/promos/validate
Preview a promo against your own order's active invoice, **reserving nothing**. Re-runs the
pricing engine over the invoice's stored lines with the candidate promo, so you see the
exact discount and resulting total before the courier re-issues with the code.
**Auth**: Bearer JWT · **Role**: CUSTOMER (the order's customer).
### Body
| field | type | required | notes |
| code | string | yes | 1–32 chars, case-insensitive |
| order_id | string (uuid) | yes | an order you own that has an active invoice |
### Success 200 — PromoPreviewResponse
| field | type | notes |
| code | string | the normalized promo code |
| discount_amount | string | the discount this promo would apply |
| original_total_amount | string | the current invoice total (no promo) |
| total_amount | string | the total after the previewed discount |
### Errors
| status | code | when |
| 404 | NOT_FOUND | you have no active invoice for that order |
| 422 | PROMO_NOT_FOUND | the code is unrecognised |
| 422 | PROMO_INACTIVE | the promo is deactivated |
| 422 | PROMO_NOT_STARTED / PROMO_EXPIRED | outside the promo's window |
| 422 | PROMO_MIN_ORDER_NOT_MET | the invoice is below the promo's minimum |
| 422 | PROMO_USAGE_EXCEEDED | the global usage cap is reached |
| 422 | PROMO_USER_LIMIT_REACHED | you have already used this promo the maximum times |
