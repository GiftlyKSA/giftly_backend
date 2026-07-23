# Money-integrity audit (current state, 2026-07-21)

The ledger and pricing engines are the crown jewels; they were re-read line-by-line,
including the SAVEPOINT and aggregate-reconcile changes from the remediation pass.
Verdict: **the invariant machinery is sound and drift-free.** The one open money finding
is not a correctness bug in the ledger — it is that a whole class of funds has no exit.

## What holds up well

- **Double entry, enforced at runtime** (`app/services/money_service.py`):
  `_validate_legs` rejects <2 legs, zero legs, and non-zero sums before any DB write;
  wallets lock FOR UPDATE in ascending-id order; the group flushes before any later
  group's lock reload. The write runs in a SAVEPOINT so a concurrent idempotency-key
  race rolls back to a clean no-op instead of a 500 — re-read this pass, the enclosing
  transaction and pre-savepoint work survive correctly.
- **Append-only ledger, trigger-enforced**: DELETE forbidden; rows immutable once not
  PENDING; status only PENDING→SETTLED|REVERSED; core columns immutable.
- **Pricing exactness** (`app/core/pricing.py`): largest-remainder discount allocation
  asserted to sum exactly; total reconstructed from its legs and compared; a failed
  invariant raises rather than returning a wrong price. The 655.50 golden example anchors
  regression.
- **Settlement is residual-based**: revenue = `total − tax − courier_payout`, so escrow
  legs reconstruct the total by construction; courier payout is on the pre-discount base
  (ADR 0005). The dispute split path bounds-checks `courier_amount ∈ [0, total]`.
- **Idempotency** on every entry point, backed by a partial unique index; the SAVEPOINT
  retry makes the true race graceful.
- **Reconciliation** (re-verified): two SQL aggregates — `settled_sums_by_wallet()`
  (one GROUP BY) and `correlation_drift_sums()` (`HAVING SUM != 0`, violators only) plus
  a distinct-count. Same semantics as the old per-wallet loop, O(1) queries.
- **Holds**: reservations under FOR UPDATE with an available-balance re-check; an
  over-release logs a WARNING before the safety clamp.

## Open findings

### NF-2 (Medium) — Money enters courier wallets but has no exit

Escrow release on completion credits the courier's wallet
(`money_service.release_escrow_on_completion`), so couriers accumulate real balance. But
the **withdrawal side is only half-built**:

- the `withdrawals` table exists (`app/models/tables.py`), the admin dashboard lists
  withdrawals read-only, `key_rotation_service` rotates withdrawal IBANs, and
  `AdminService.reveal_iban` decrypts one for a stepped-up admin;
- yet **no service, repository, or route constructs a `Withdrawal`** (the only
  `Withdrawal(...)` outside the model is in a test), there is **no state transition** out
  of `REQUESTED`, and **no ledger method** debits a courier wallet to pay one out;
- `MIN_WITHDRAWAL_AMOUNT` is defined in settings and read nowhere.

So a courier can earn indefinitely and never cash out, and an admin can view/reveal a
withdrawal that nothing can create. This is not a ledger-correctness defect — no money is
mis-moved — but it is a real product gap and a block of dead surface (config + admin
views + encryption path) with no live counterpart.

**Recommendation**: decide the scope. Either (a) implement the flow — a courier
`request_withdrawal` (validate `MIN_WITHDRAWAL_AMOUNT` and available balance, encrypt the
IBAN, place a hold), and an admin settle path that posts a balanced group
(courier wallet → SYSTEM_GATEWAY) and flips the status — with reconcile-backed tests; or
(b) explicitly defer it in DECISIONS.md and guard/remove the dead `MIN_WITHDRAWAL_AMOUNT`
and the admin withdrawal views so the surface doesn't imply a capability that isn't there.

## Accepted trade-offs (re-confirmed)

- **AT-3** — `statement_cache_size=0` for PgBouncer transaction-mode safety; revisit only
  if PgBouncer is dropped or moved to session mode.

## Verified invariant inventory

| Invariant | Enforced by |
| --- | --- |
| Every correlation group sums to 0.00 | `_validate_legs` + SQL `HAVING` reconcile |
| Wallet `balance == SUM(settled)` | Single write path (`post_group`) + `reconcile()` |
| Ledger rows immutable / append-only | DB triggers |
| Invoice items frozen once not DRAFT | DB trigger `enforce_invoice_item_freeze` |
| Discount allocations sum exactly | `PricingIntegrityError` assert |
| Escrow release legs reconstruct total | Residual construction in `compute_settlement` |
| Dispute split ∈ [0, total] | `_apply_dispute_outcome` guard (422) |
| No double settlement of an intent | IP gate + HMAC + Redis lock + status + amount + idempotency + SAVEPOINT |
| Held funds never exceed balance | FOR UPDATE re-check + DB CHECK + loud clamp |
