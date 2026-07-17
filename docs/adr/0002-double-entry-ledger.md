# ADR 0002 — Double-entry ledger as the only truth about money

## Status
Accepted (2026-07-17)

## Context
Money must never be silently created or destroyed across top-ups, escrow holds,
settlements, payouts, refunds, commission, tax, and promo subsidies.

## Decision
An append-only `transactions` table is the sole source of truth. Every movement writes
≥ 2 rows sharing one `correlation_id` whose signed amounts sum to 0.00. External flows
(gateway in/out) balance against a `SYSTEM_GATEWAY` wallet; VAT accrues to a separate
`SYSTEM_TAX_PAYABLE` wallet, never revenue. `UPDATE`/`DELETE` are forbidden by trigger,
except the single `PENDING → SETTLED|REVERSED` status transition; corrections are
compensating entries.

## Consequences
- `balance == SUM(settled amounts)` holds for every wallet and is asserted at runtime
  and by the nightly reconciliation job, which pages on any drift.
- Mistakes are auditable and reversible without editing history.
- Every wallet write and its ledger rows occur in one transaction with
  `SELECT ... FOR UPDATE` in ascending wallet-id order to avoid deadlocks.
