# Schema overview

22 application tables. Every table has `id` (UUID PK), `created_at`, `updated_at`
(trigger-maintained). Soft-deletable tables also carry `deleted_at`. Money is
`NUMERIC(12,2)`; spatial columns are `GEOMETRY(Point, 4326)`; enums are native PG enums.

## Key relationships

| parent | child | rule |
| --- | --- | --- |
| users | courier_profiles | 1–0..1, PK=FK, cascade |
| users | wallets | 1–0..1, **restrict** (never delete a user holding money) |
| users | orders (customer_id) | 1–0..*, restrict |
| users | orders (courier_id) | 0..1–0..*, restrict |
| orders | invoices | 1–0..*, cascade, ≤1 active (partial unique index) |
| orders | conversations | 1–0..1, cascade, unique |
| orders | disputes | 1–0..1, unique |
| invoices | invoice_items | 1–1..*, cascade, frozen after ISSUED |
| invoices | promo_redemptions | 1–0..1 |
| promos | promo_redemptions | 1–0..*, **restrict** (keep history) |
| payment_intents | wallet_topups | 1–0..1, unique |
| conversations | messages | 1–0..*, cascade, append-only |
| wallets | transactions | 1–0..*, **restrict** (ledger never orphaned/cascaded) |

## System wallets (seeded by migration)

`SYSTEM_ESCROW`, `SYSTEM_REVENUE`, `SYSTEM_GATEWAY`, `SYSTEM_TAX_PAYABLE` — one each,
enforced by partial unique indexes. `SYSTEM_GATEWAY` and `SYSTEM_REVENUE` may go
negative and are excluded from the non-negative balance CHECK.

## Enforced-in-DB invariants (a sample)

- `transactions`: append-only trigger; `amount <> 0`; unique `idempotency_key`.
- `invoices`: `net_after_discount = items + courier + service − discount`;
  `total = net_after_discount + tax`; promo/discount pairing; one active per order.
- `invoice_items`: `line_net = unit * qty`; frozen unless parent is DRAFT.
- `promos`: `code = upper(btrim(code))`; usage bounds; value-by-type.
- `orders`: delivery date within [today, +180d]; courier required after assignment.
- `wallets`: user/system pairing; non-negative balance except gateway/revenue.

See `app/models/tables.py` for the authoritative column, constraint, and index list, and
the baseline Alembic migration for the DDL and triggers.
