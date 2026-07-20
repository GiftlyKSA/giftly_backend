# Logic & correctness audit

## What holds up well

- **Order state machine**: transitions go through `assert_transition`
  (`app/services/order_state.py`), raising `InvalidStateTransitionError` (409) on any
  illegal move; payment settles the order to IN_PROGRESS inside the same transaction as
  the invoice flip (`app/services/payment_service.py:343-353`), so the two can never
  diverge.
- **Race handling is layered, not hopeful**: the courier accept race uses a Redis lock
  *plus* a conditional UPDATE; promo caps use a single conditional
  `UPDATE ... RETURNING` (`app/repositories/promo_repository.py:93-110`) so "first 20"
  can never overshoot; approve vs auto-approve is settled by the order-keyed ledger
  idempotency key; webhook replays are serialized by transaction-number lock and intent
  status.
- **Expiry design avoids the late-webhook race**: order-payment expiry is driven off
  the *invoice* (not the intent), so a webhook that arrives after the sweeper reopened
  the order finds the intent already EXPIRED and no-ops — the documented reasoning
  (DECISIONS.md 2026-07-19) matches the implementation (`app/workers/expiry.py`).
- **Workers are crash-tolerant**: each sweep processes rows in their own transactions
  with FOR UPDATE re-checks, so a mid-sweep crash loses no row and re-processes none;
  receipts are send-then-stamp (at-least-once, deduped by pass-level FOR UPDATE).
- **Geospatial traps respected**: `ST_MakePoint` is longitude-first
  (`app/repositories/order_repository.py:60,89,211`) and distance casts both sides to
  `geography` for metres (`order_repository.py:214`) — both of the repo's named traps
  are handled at every call site.
- **WS lifecycle**: the pubsub reader task is cancelled and awaited with
  `CancelledError` explicitly suppressed (`app/routers/chat.py:186-191`) — the
  not-an-`Exception`-subclass pitfall is handled; membership is checked *before*
  `accept()`, and non-members are closed with distinct policy codes (4401/4403).

## Findings

### LOG-1 (Medium) — WebSocket sends never trigger a push notification

The REST send path notifies the recipient at the router boundary
(`app/routers/chat.py:138-141`), but the WS receive loop constructs `ChatService`
directly and calls `send_message` with no `NotificationService`
(`app/routers/chat.py:201-220`). Consequence: when the sender uses the socket and the
recipient is offline (the exact case push exists for), no "New message" push is sent.
The two send paths silently disagree on a user-visible behaviour.

**Recommendation**: after a successful WS `send_message`, fire the same
`notify_user(recipient, "New message", ...)` call the REST route makes (best-effort,
as elsewhere). One helper shared by both paths would prevent future drift.

### LOG-2 (Low) — WS membership is checked only at connect time

`conversation_ws` verifies participation once (`app/routers/chat.py:170-174`) and the
socket then lives indefinitely. There is no flow today that removes a participant from
a conversation (members are fixed by the order), so this is currently unreachable —
but if account banning starts closing conversations (see SEC-1), long-lived sockets
will outlive their authorization. Worth a re-check on a timer or on each inbound send
once SEC-1 is fixed.

### LOG-3 (Low) — `_extract_text` accepts any raw frame as message text

`app/routers/chat.py:222-231`: a frame that fails JSON parsing is treated as the
message text itself (`return raw.strip()`). Lenient-in is fine, but it means a client
bug that sends, say, a binary blob's stringification becomes a persisted, encrypted
message. Schema-validating WS frames (reject non-JSON) would match the REST contract,
where `SendMessageRequest` enforces shape and length — today the WS path has **no
length cap at all** (ties into SEC-4).

### LOG-4 (Info) — Readiness probe opens a session per check

`app/routers/health.py:28-37` runs `SELECT 1` through a fresh session from the shared
factory. Correct, and pool checkout makes it cheap; noted only because orchestrators
can poll readiness aggressively — the health exemption from the rate limiter
(`app/main.py:141`) makes this path unthrottled by design, so a public deployment
should keep `/api/health/ready` behind the load balancer, not the open internet.

### LOG-5 (Info) — Notification fan-out is serial per token batch

`NotificationService` collects tokens and sends via one push call; city-wide broadcasts
(`notify_city_couriers`) go through a single `send_push(tokens, ...)` — fine for the
fake and for FCM multicast, but worth confirming the real client chunks token lists to
the provider's per-request cap (FCM: 500) once real credentials land.

## Test-suite observations

- 210 tests, 87.9 % coverage, all money paths re-reconciled after mutation — strong.
- One pre-existing cross-test flake: `test_create_rejects_second_active_invoice`
  (`tests/integration/test_invoice_service.py:114`) fails rarely in full-suite runs and
  passes 10/10 in isolation; it interacts with committed rows from concurrency tests
  sharing the DB. Not addressed in this audit; worth a follow-up with a unique-order
  fixture.
- Worker tests exercise the real DB path (own-engine fallback included) rather than
  mocking — this caught the orphan-wallet invariant violation during Phase 13 and is
  the right trade.
