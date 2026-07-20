# Money-integrity audit (re-audited 2026-07-20 after the fix batch)

The ledger and pricing engines are the crown jewels; they were read line-by-line.
Verdict: **no High or Medium money finding existed, and the two Low findings are
fixed**. The invariants are enforced in four independent layers (service assertions,
DB CHECK constraints, append-only triggers, and nightly reconciliation), and every
money-moving test re-runs reconciliation to prove no drift.

## What holds up well

- **Double entry, enforced at runtime** (`app/services/money_service.py`):
  `_validate_legs` refuses <2 legs, zero legs, and non-zero sums *before* touching the
  DB; wallets are locked FOR UPDATE in ascending-id order (deadlock-free); the group
  flushes before any later group's lock reload so in-memory balances are never
  silently discarded.
- **Append-only ledger, trigger-enforced** (baseline migration): DELETE forbidden;
  rows immutable once not PENDING; status may only move PENDING→SETTLED|REVERSED;
  amount/wallet/type/correlation immutable. Application bugs cannot rewrite history.
- **Pricing exactness** (`app/core/pricing.py`): largest-remainder discount allocation
  asserted to sum exactly; the total reconstructed from its legs and compared; a failed
  invariant raises `PricingIntegrityError` rather than returning a wrong price. The
  655.50 golden example anchors regression.
- **Settlement is residual-based**: platform revenue is `total − tax − courier_payout`,
  so escrow-release legs always reconstruct the escrow total by construction. Courier
  payout on the pre-discount base (ADR 0005) is implemented as specified.
- **Idempotency keys on every money entry point**, backed by a partial unique index —
  webhook replays and approve/auto-approve races post exactly once.
- **Hold discipline**: holds are reservations under FOR UPDATE with an
  available-balance re-check; `fund_escrow_for_invoice` releases the hold only on the
  first successful post, never on a replay.
- **Webhook settlement layering**: source-IP gate → raw-body HMAC → Redis lock on the
  transaction number → intent status check under FOR UPDATE → amount equality check →
  ledger idempotency keys. Six independent gates after the fix batch.

## Findings and their resolutions

### MON-1 (Low) — FIXED — Concurrent idempotent posts degrade to a no-op

`post_group` now runs its lock/apply/flush inside a **SAVEPOINT**
(`WalletRepository.savepoint()`); if a truly concurrent group with the same
idempotency key wins the unique-index race, the `IntegrityError` rolls back to the
savepoint, the key is re-checked, and the call returns `False` (replay) exactly like
the fast-path detection — instead of surfacing an opaque 500. The enclosing
transaction (and any work the caller did before the group) survives. The invariant was
never at risk; this fix removes the ugly failure mode.

### MON-2 (Low) — FIXED — Hold over-release is loud

`release_hold` still clamps `held_balance` at zero (the CHECK constraint must never
trip), but a release larger than the outstanding hold now logs a **WARNING** naming
the wallet and both amounts — a clamp that fires is always a bug upstream, and it is
no longer silent.

### MON-3 (Info) — Accepted — `statement_cache_size=0`

asyncpg's prepared-statement cache stays disabled for PgBouncer transaction-mode
safety. Re-confirmed; revisit only if PgBouncer is dropped or moved to session mode.

## Reconciliation (changed by PERF-2, re-verified here)

`MoneyService.reconcile` now consumes two SQL aggregates —
`settled_sums_by_wallet()` (one GROUP BY for every wallet's settled sum) and
`correlation_drift_sums()` (`GROUP BY ... HAVING SUM != 0`, returning **only**
violators) — plus a distinct-count for the report. Semantics are identical: the same
drift strings, the same wallet/correlation counts, proven by the pre-existing
reconcile tests which pass unchanged. Wallets with no transactions compare against an
implicit 0.00.

## Verified invariant inventory

| Invariant | Enforced by |
| --- | --- |
| Every correlation group sums to 0.00 | Runtime assert (`_validate_legs`) + SQL-side `HAVING` check |
| Wallet `balance == SUM(settled)` | Single write path (`post_group`) + `reconcile()` |
| Ledger rows immutable / append-only | DB triggers (baseline migration) |
| Invoice items frozen once not DRAFT | DB trigger `enforce_invoice_item_freeze` |
| Discount allocations sum exactly | `PricingIntegrityError` assert |
| Escrow release legs reconstruct total | Residual construction in `compute_settlement` |
| No double settlement of an intent | IP gate + HMAC + Redis lock + status + amount + idempotency keys |
| Held funds never exceed balance | FOR UPDATE re-check in `hold_funds` + DB CHECK + loud clamp |
