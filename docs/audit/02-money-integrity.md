# Money-integrity audit

The ledger and pricing engines are the crown jewels; they were read line-by-line.
Verdict up front: **no High or Medium money finding**. The invariants are enforced in
four independent layers (service assertions, DB CHECK constraints, append-only
triggers, and nightly reconciliation), and every money-moving test re-runs
reconciliation to prove no drift.

## What holds up well

- **Double entry, enforced at runtime** (`app/services/money_service.py:77-134`):
  `post_group` refuses <2 legs, zero legs, and non-zero sums *before* touching the DB;
  wallets are locked FOR UPDATE in ascending-id order (deadlock-free); the group flushes
  before any later group's lock reload so in-memory balances are never silently
  discarded (`money_service.py:130-133` — a real bug class, correctly pre-empted).
- **Append-only ledger, trigger-enforced**
  (`app/migrations/versions/0013a70d0480_baseline_schema.py:86-99`): DELETE forbidden;
  rows immutable once not PENDING; status may only move PENDING→SETTLED|REVERSED;
  amount/wallet/type/correlation immutable. Application bugs cannot rewrite history.
- **Pricing exactness** (`app/core/pricing.py`): discount allocation uses
  largest-remainder correction and then *asserts* the allocations sum exactly
  (`pricing.py:320-322`); the total is reconstructed from its legs and compared
  (`pricing.py:323-329`); a failed invariant raises `PricingIntegrityError` rather than
  returning a wrong price. The 655.50 golden example anchors regression.
- **Settlement is residual-based** (`pricing.py:136-162`): platform revenue is defined
  as `total − tax − courier_payout`, so the escrow-release legs always reconstruct the
  escrow total by construction — no rounding path can strand a halala in escrow.
  Courier payout on the pre-discount base (ADR 0005) is implemented as specified.
- **Idempotency keys on every money entry point**: topup `intent:{id}:topup`, escrow
  funding `invoice:{id}:escrow`, release `order:{id}:release`, refund
  `order:{id}:refund`, split `order:{id}:split` — each group keyed on its natural
  aggregate so webhook replays and approve/auto-approve races post exactly once, backed
  by a partial unique index (`app/models/tables.py:798-801`).
- **Hold discipline** (`money_service.py:320-346`): holds are reservations
  (`held_balance`), not ledger movements, taken under FOR UPDATE with an
  available-balance re-check — the wallet invariant `balance == SUM(settled)` is never
  touched by a hold, and `fund_escrow_for_invoice` releases the hold only on the first
  successful post, never on a replay (`money_service.py:414-417`).
- **Webhook settlement layering** (`app/services/payment_service.py:260-297`): raw-body
  HMAC → Redis lock on the transaction number → intent status check under
  FOR UPDATE → amount equality check against the intent → ledger idempotency keys.
  Five independent gates; any one alone would prevent double-credit.

## Findings

### MON-1 (Low) — Concurrent idempotent posts fail loudly instead of gracefully

`post_group`'s replay detection is SELECT-then-INSERT
(`money_service.py:105-110`): two truly concurrent groups with the same idempotency key
can both pass the existence check; the partial unique index then rejects the second
with an IntegrityError → 500 and rollback. **The invariant holds — no double post is
possible** — but the second caller gets an opaque 500 instead of the "already posted"
no-op. In practice the Redis webhook lock and FOR UPDATE intent lock serialize the
realistic paths, so this needs both a lock-TTL expiry *and* sub-millisecond timing.

**Recommendation**: catch `IntegrityError` on the flush inside `post_group`, re-check
the key, and return `False` (replay) when it now exists.

### MON-2 (Low) — `release_hold` clamps at zero rather than asserting

`money_service.py:344`: `held_balance = max(ZERO, held − amount)`. The clamp guarantees
the CHECK constraint never trips, but it also means a double-release (a bug) silently
zeroes instead of raising — and could free a *different* pending payment's reservation,
letting available balance be promised twice. The current call sites are disciplined
(release exactly once per hold, guarded by `posted and was_held` or the expiry
sweeper's FOR UPDATE state check), so this is latent, not live.

**Recommendation**: log at WARNING (or raise in non-production) when
`amount > held_balance` — a clamp that fires is always a bug elsewhere.

### MON-3 (Info) — `statement_cache_size=0`

`app/core/db.py:30`: asyncpg's prepared-statement cache is disabled for PgBouncer
transaction-mode safety. Correct and documented; the cost is re-parse per statement.
If PgBouncer is ever dropped (or moved to session mode), this knob is worth revisiting.

## Verified invariant inventory

| Invariant | Enforced by |
| --- | --- |
| Every correlation group sums to 0.00 | Runtime assert (`money_service.py:98-100`) + `reconcile()` |
| Wallet `balance == SUM(settled)` | Single write path (`post_group`) + `reconcile()` |
| Ledger rows immutable / append-only | DB triggers (baseline migration :86-99) |
| Invoice items frozen once not DRAFT | DB trigger `enforce_invoice_item_freeze` |
| Discount allocations sum exactly | `PricingIntegrityError` assert (`pricing.py:320`) |
| Escrow release legs reconstruct total | Residual construction (`pricing.py:156`) |
| No double settlement of an intent | Redis lock + status check + amount check + idempotency keys |
| Held funds never exceed balance | FOR UPDATE re-check in `hold_funds` + DB CHECK |
