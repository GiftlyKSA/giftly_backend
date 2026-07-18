# Flow: promos

Promo codes are case-insensitive (stored and matched as `strip().upper()`). A promo has
an optional global cap (`max_total_usages`, the "first 20"), a per-user cap
(`max_usages_per_user`), an active window, and a minimum order amount.

## Validation (read-only, no reservation)

`PromoService.validate(code, discountable_base, user_id)` returns the would-be discount
or raises a precise error (each a 422 with its own `code`):

| code | when |
| PROMO_NOT_FOUND | no promo for the normalized code |
| PROMO_INACTIVE | `is_active = false` |
| PROMO_NOT_STARTED | before `starts_at` |
| PROMO_EXPIRED | at/after `ends_at` |
| PROMO_MIN_ORDER_NOT_MET | base below `min_order_amount` |
| PROMO_USAGE_EXCEEDED | global cap reached |
| PROMO_USER_LIMIT_REACHED | this user is at their per-user cap |

The discountable base is `items_net + courier_fee_net` (the service fee is never
discounted). The discount itself is computed by the one pricing routine
(`compute_promo_discount`), so validation and the invoice total always agree.

## Reservation lifecycle

- **RESERVED** — at invoice *issue*. A single atomic `UPDATE promos SET used_count =
  used_count + 1 WHERE ... used_count < max_total_usages RETURNING used_count` claims a
  slot; no row returned ⇒ `PROMO_USAGE_EXCEEDED`. The per-user cap is enforced in the
  same transaction (the UPDATE row-locks the promo), then a RESERVED `promo_redemptions`
  row is inserted. A SELECT-then-UPDATE would let concurrent requests overshoot the cap
  — the whole reason for the atomic form.
- **CONSUMED** — on invoice *PAID*.
- **RELEASED** — on invoice cancel/expiry, order cancel before payment, or a full refund.
  Release is an atomic `used_count - 1` plus the row → RELEASED, returning the slot to
  the pool so an abandoned checkout never permanently burns a slot (otherwise "first 20"
  quietly becomes "first 6").

The nightly reconciliation asserts `used_count == COUNT(redemptions WHERE status <>
'RELEASED')`.
