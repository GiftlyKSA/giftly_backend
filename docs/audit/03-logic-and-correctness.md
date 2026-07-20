# Logic & correctness audit (re-audited 2026-07-20 after the fix batch)

## What holds up well

- **Order state machine**: transitions go through `assert_transition`, raising
  `InvalidStateTransitionError` (409) on any illegal move; payment settles the order to
  IN_PROGRESS inside the same transaction as the invoice flip, so the two can never
  diverge.
- **Race handling is layered, not hopeful**: the courier accept race uses a Redis lock
  *plus* a conditional UPDATE; promo caps use a single conditional
  `UPDATE ... RETURNING`; approve vs auto-approve is settled by the order-keyed ledger
  idempotency key; webhook replays are serialized by transaction-number lock and intent
  status — and a same-millisecond idempotency race now degrades to a no-op (MON-1).
- **Expiry design avoids the late-webhook race**: order-payment expiry is driven off
  the *invoice*, so a webhook that arrives after the sweeper reopened the order finds
  the intent already EXPIRED and no-ops.
- **Workers are crash-tolerant**: each sweep processes rows in their own transactions
  with FOR UPDATE re-checks; receipts are send-then-stamp (at-least-once, deduped).
  Locks are now the Lua compare-and-delete kind everywhere (SEC-5).
- **Geospatial traps respected**: `ST_MakePoint` is longitude-first at every call
  site, and distance casts both sides to `geography` for metres.
- **WS lifecycle**: the pubsub reader task is cancelled and awaited with
  `CancelledError` explicitly suppressed; membership is checked before `accept()`;
  non-members get distinct close codes (4401 unauthenticated / 4403 not a member).

## Findings and their resolutions

### LOG-1 (Medium) — FIXED — WS sends now push the recipient

`_pump_socket_to_chat` (`app/routers/chat.py`) fires the same best-effort
`notify_user(recipient, "New message", ...)` the REST route fires after every
persisted WS message — with no message text in the push body, matching the Restricted
data rule. The recipient is resolved once at connect time from the conversation row.
Verified by `test_websocket_send_persists_notifies_and_guards`, which asserts exactly
one push and that the body carries no message content.

### LOG-2 (Low) — FIXED — A mid-connection ban closes the socket

The WS receive loop checks the `auth:banned:<user_id>` flag (set by SEC-1's ban fix)
on each inbound frame and closes the socket with 4401. A socket that never speaks
again idles until disconnect, which is harmless — its user cannot send, and inbound
delivery only carries what conversation members could read anyway.

### LOG-3 (Low) — FIXED — WS frames must be JSON

`_extract_text` now returns "" for anything that is not a JSON object with a `text`
field — the "any raw frame becomes a message" behaviour is gone, and the WS contract
matches the REST schema. Combined with the `WS_MAX_FRAME_BYTES` cap (SEC-4), a client
bug can no longer persist arbitrary blobs as encrypted messages. Verified by the same
WS test (a raw-text frame and an oversized frame both leave no trace).

### LOG-4 (Info) — Accepted — Readiness probe is unthrottled by design

`/api/health/ready` opens a pooled session for `SELECT 1` and pings Redis; it is
exempt from the rate limiter so an orchestrator can always reach it. Keep it behind
the load balancer rather than the open internet. Unchanged, deliberately.

### LOG-5 (Info) — Open (vendor-blocked) — Push token chunking

`notify_city_couriers` hands the full token list to one `send_push` call. The fake and
a multicast-capable backend are fine; whether the real Supabase/FCM function needs
client-side chunking (FCM caps at 500 tokens/request) cannot be verified until real
credentials land. Tracked here; revisit when the vendor contract is finalised.

## Test-suite observations

- 217 tests, 87.9 % coverage, all money paths re-reconciled after mutation.
- The fix batch added end-to-end coverage for: ban revocation (access, refresh, and
  re-login), webhook IP gating (both directions), the OTP key chain, chunked-body
  rejection, WS guards + WS push parity, and the refresh-token purge.
- One pre-existing cross-test flake remains known:
  `test_create_rejects_second_active_invoice` fails rarely in full-suite runs and
  passes consistently in isolation (committed-row interaction from concurrency tests
  sharing the DB). It did not fire in the post-fix runs; still worth a unique-fixture
  follow-up.
- Worker tests exercise the real DB path (own-engine fallback included) rather than
  mocking — this previously caught a global-invariant violation and remains the right
  trade.
