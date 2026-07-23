# Logic & correctness audit (current state, 2026-07-21)

## What holds up well

- **Order state machine**: transitions go through `assert_transition` (409 on any
  illegal move); payment settles the order to IN_PROGRESS in the same transaction as the
  invoice flip, so the two can't diverge.
- **Race handling is layered, not hopeful**: courier accept uses a Redis lock plus a
  conditional UPDATE; promo caps use a single conditional `UPDATE ... RETURNING`; approve
  vs auto-approve is settled by the order-keyed ledger idempotency key; webhook replays
  are serialized by transaction-number lock and intent status; and a same-millisecond
  idempotency race degrades to a SAVEPOINT no-op.
- **Expiry avoids the late-webhook race**: order-payment expiry is driven off the invoice,
  so a webhook arriving after the sweeper reopened the order finds the intent EXPIRED and
  no-ops.
- **Workers are crash-tolerant**: per-row transactions with FOR UPDATE re-checks; receipts
  are send-then-stamp (deduped). All five scheduled jobs (reconcile, receipts,
  auto-approve, expiry, refresh-token purge) take their lock via the Lua
  compare-and-delete `redis_lock`.
- **Geospatial traps respected**: `ST_MakePoint` is longitude-first at every call site;
  distance casts both operands to `geography` for metres.
- **Chat WS**: membership checked before `accept()`; distinct close codes (4401/4403);
  the pubsub reader task is cancelled and awaited with `CancelledError` suppressed; inbound
  frames are JSON-only, size-capped, per-user throttled, and a mid-connection ban closes
  the socket; a WS send fires the same text-free recipient push the REST path does.

## Open findings

### NF-3 (Low) — Two Redis round trips per inbound WS frame

`_pump_socket_to_chat` (`app/routers/chat.py`) does a `GET auth:banned:<id>` and then a
rate-limiter eval on every received frame before persisting. Both are correct and cheap,
but they are separate round trips on the hot path of a busy conversation. If WS
throughput ever matters, fold the ban check into the rate-limit Lua (it already touches
Redis), or check the ban flag on a coarser cadence than every frame. Not worth changing
at target scale — noted for when it is.

### NF-6 (Info) — One known cross-test flake

`test_create_rejects_second_active_invoice` (`tests/integration/test_invoice_service.py`)
fails rarely under full-suite runs and passes consistently in isolation — it interacts
with committed rows from concurrency/worker tests that share the database. It did not fire
in the recent green runs. The durable fix is a per-test unique order/invoice fixture so it
cannot collide with committed state; left open because it is a test-hygiene item, not a
product defect.

## Feature-completeness note

The withdrawal gap (NF-2, detailed in money-integrity) is as much a completeness finding
as a money one: the read/model/encryption surface exists with no write path. It is the
only place in the codebase where a persisted model has no live creator.

## Test-suite observations

- ~88 % coverage; every money path re-runs `reconcile()` after mutation.
- End-to-end coverage exists for the security-sensitive flows: ban revocation (access,
  refresh, re-login), webhook IP gating (both directions), the OTP key chain,
  chunked-body rejection, WS guards + WS→push parity, and the refresh-token purge.
- Worker tests exercise the real DB path (own-engine fallback) rather than mocking — this
  previously caught a global-invariant violation and remains the right trade.
