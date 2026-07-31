# Money-integrity audit (2026-07-31)

## Verdict

The append-only double-entry ledger, pricing engine, escrow settlement, refunds,
disputes, top-ups, and new courier withdrawals preserve the core invariants. No path
creates or destroys value. The remaining weakness is visibility into non-ledger holds.

## Verified invariants

| Invariant | Enforcement |
| --- | --- |
| Every posted group has ≥2 non-zero legs and sums to 0.00 | `MoneyService._validate_legs` before write |
| Wallet balance equals settled ledger sum | single posting path plus reconciliation aggregate |
| Ledger is append-only | PostgreSQL triggers and ORM write surface |
| Pricing uses Decimal and one quantizer | `core/money.py` + `core/pricing.py` |
| Discounts allocate exactly | largest-remainder allocation and integrity exceptions |
| Escrow release reconstructs invoice total | residual platform revenue calculation |
| Duplicate payments/settlements are no-ops | stable unique idempotency keys + savepoints |
| Concurrent wallet debits serialize | ascending `SELECT ... FOR UPDATE` locks |

## Withdrawal flow verified

- Courier request validates configured bounds and available balance, encrypts the IBAN,
  and increases `held_balance` under the wallet lock.
- A courier-scoped idempotency key and unique partial index make request retries return
  the original row without duplicating the hold, including the constraint-race path.
- Admin decisions lock the withdrawal row. Rejection releases the hold; payment requires
  approval, debits the courier wallet, credits `SYSTEM_GATEWAY`, releases the hold, and
  marks `PAID` in one database transaction.
- Repeated approve/reject/paid actions are idempotent. Audit rows record each first
  transition. Reconciliation remains clean after payout.

## Finding

### OPEN-3 (Medium) — held balances are not independently reconciled

`held_balance` is intentionally outside the append-only transaction sum because a hold
is a reservation, not a movement. Row locks prevent oversubscription, and invoice and
withdrawal paths release their own holds transactionally. The nightly reconciler checks
ledger balances and zero-sum groups, but it does not derive the expected hold total from
pending split payments and `REQUESTED|APPROVED` withdrawals. A future bug or manual DB
repair could therefore strand availability without triggering reconciliation.

Add an aggregate hold report and alert before meaningful transaction volume. Do not
convert holds into settled ledger entries; that would change the accounting model.

## Operational notes

- `APPROVED` withdrawals intentionally retain funds until an external transfer succeeds
  or an admin rejects them. Alert on old approved rows.
- `SYSTEM_GATEWAY` may be negative and represents the platform boundary: top-ups debit
  it; paid withdrawals credit it.
- `statement_cache_size=0` remains an accepted PgBouncer transaction-mode trade-off.
