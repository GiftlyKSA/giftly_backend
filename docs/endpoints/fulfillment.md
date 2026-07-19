# Fulfilment: delivery, approval, disputes, ratings

The escrow lifecycle. Money moves only through the double-entry ledger; the actor id
always comes from the JWT. All money is a decimal STRING.

## POST /api/orders/{order_id}/deliver
Mark an in-progress order delivered with geofenced proof. **Role**: COURIER (the assigned
courier). Order `IN_PROGRESS -> DELIVERED`.
### Body
| field | type | notes |
| latitude / longitude | number | the courier's current position |
| proof_media_keys | array<string> | 1–5 confirmed `orders/proof/...` keys |
| note | string | optional, <= 500 chars |
The courier must be within `MAX_DELIVERY_RADIUS_METERS` (200 m) of the drop-off; the
capture location is stored on each proof photo. Distance is computed on `geography`
(metres), not `geometry` (degrees).
### Errors
| status | code | when |
| 404 | NOT_FOUND | not your assigned order |
| 409 | INVALID_STATE_TRANSITION | the order is not IN_PROGRESS |
| 422 | VALIDATION_ERROR | outside the radius, no/too many photos, bad proof object |

## POST /api/orders/{order_id}/approve
Approve a delivered order. **Role**: CUSTOMER. Order `DELIVERED -> COMPLETED` and escrow
is RELEASED: the courier is paid on the pre-discount base minus commission, tax accrues to
`SYSTEM_TAX_PAYABLE`, and the platform keeps the residue (which funds any promo). Release
is idempotent, so a customer approval racing the auto-approve job pays out exactly once.

If the customer never acts, a delivered order auto-approves after `AUTO_APPROVE_HOURS`
(72 h) via a scheduled sweeper.

## POST /api/orders/{order_id}/dispute
Open a dispute, freezing escrow. **Role**: participant (CUSTOMER or COURIER). Order
`IN_PROGRESS|DELIVERED -> DISPUTED`. One dispute per order (a second is 409).

## POST /api/admin/disputes/{dispute_id}/resolve
Resolve a dispute, moving escrow. **Role**: ADMIN (JWT). Distinct from the read-only HTML
admin dashboard.
### Body
| field | notes |
| outcome | `RESOLVED_CUSTOMER` (full refund → REFUNDED), `RESOLVED_COURIER` (normal payout → COMPLETED), or `RESOLVED_SPLIT` |
| note | optional resolution note |
| courier_amount | required for SPLIT — the courier's share (0..total); the rest is refunded, platform books nothing |

## POST /api/orders/{order_id}/ratings
Rate the other party on a completed order. **Role**: participant. One rating per rater per
order; the rated user is derived from the order, never the body.
### Body
| field | notes |
| score | integer 1–5 |
| comment | optional, <= 500 chars |
### Errors
| status | code | when |
| 404 | NOT_FOUND | not a participant's order |
| 409 | CONFLICT | the order is not completed, or you already rated it |

## GET /api/users/{user_id}/ratings/summary
A user's aggregate received rating. **Auth**: any authenticated user.
Returns `{ user_id, average_score, count }`.

## The settlement split (the 655.50 golden example)
Escrow held 655.50 releases as: courier **540.00** (600 base − 60 commission), VAT
**85.50**, platform revenue **30.00** (service fee 30 + commission 60 − promo subsidy 60).
See ADR 0005 — the courier is paid on the pre-discount base, so a promo never underpays
the courier; the platform funds it.
