# Performance audit (re-audited 2026-07-20 after the fix batch)

Context: a city-scoped gifting marketplace — hundreds of requests/second is the
realistic ceiling, not tens of thousands. Every graded finding from the first pass is
now fixed; what remains is the accepted-trade-off list.

## What holds up well

- **Index coverage matches the query surface**: composite indexes on
  `(delivery_city, status)`, `(customer_id, created_at DESC)`, a GiST index on
  `delivery_location`, partial indexes on nullable uniques, and worker-sweep indexes.
  The sweepers are index-backed with LIMIT batches, not table scans.
- **Pagination is capped everywhere**: every list route validates `limit` with
  `Query(ge=1, le=100)` and uses keyset cursors, not OFFSET.
- **The event loop is respected**: every real integration is async httpx or aioboto3;
  no sync SDK call exists in an async path.
- **Hot money paths are lean**: `post_group` is one SELECT FOR UPDATE + N inserts +
  one flush (now inside a savepoint, which on Postgres is a near-free nested marker).

## Findings and their resolutions

### PERF-1 (Medium) — FIXED — Pooled HTTP clients everywhere

Each real integration client (`paylink`, `push`, `sms`, `sndr` email) now creates
**one** `httpx.AsyncClient` in `__init__` and reuses it for every call — connection
pooling and TLS session reuse instead of a handshake per gateway charge/SMS/push/
email. Each exposes `aclose()`, and a new app shutdown hook (`_install_shutdown` in
`app/main.py`) closes the clients, the Redis pool, and the engine. Fakes are skipped
via duck-typing.

### PERF-2 (Medium) — FIXED — Reconciliation is O(1) queries, O(violations) memory

`reconcile()` now runs `settled_sums_by_wallet()` (one GROUP BY over settled
transactions) instead of a SUM per wallet, and `correlation_drift_sums()`
(`GROUP BY correlation_id HAVING SUM(amount) != 0`) so only violators — normally zero
rows — cross the wire, plus one distinct-count for the report. The nightly job no
longer materialises the entire ledger's correlation map in Python. A `checked_through`
watermark remains a possible future refinement if the ledger reaches the tens of
millions of rows; not needed at target scale.

### PERF-3 (Low) — FIXED — `refresh_tokens` is bounded

The nightly `run_purge_refresh_tokens` task (Redis-locked, own-engine pattern like the
other sweeps) deletes rows expired for longer than `REFRESH_TOKEN_RETENTION_DAYS`
(env var, default 30) — keeping a full forensic window for reuse detection while
bounding growth. Verified by `test_purge_refresh_tokens_deletes_only_long_expired`.
`admin_sessions` and `audit_log` retention remain deliberate keep-forever choices for
now (audit trail).

### PERF-4 (Low) — FIXED — Guard middlewares merged

The rate limiter and body-size guard are now one `_request_guards` middleware
(body check first — it is free — then the throttle), taking the stack from four
`BaseHTTPMiddleware` layers to three. Pure-ASGI implementations remain the next step
if p99 latency ever matters; not warranted today.

### PERF-5 (Info) — FIXED (free with SEC-6) — One round trip per throttle check

The Lua window script folded INCR + EXPIRE + TTL into a single eval, so the happy path
costs exactly one Redis round trip and the blocked path the same.

### PERF-6 (Info) — Accepted — Per-request service construction

Services and repositories are thin objects constructed per request; the pattern buys
session-scoped correctness for negligible cost. Noted so nobody "optimises" it into
shared-session bugs.

## Capacity notes

- Uvicorn workers behind Gunicorn, async DB pool with `pool_pre_ping`: standard and
  sound. Pool sizing is left to SQLAlchemy defaults (5 + 10 overflow per worker) —
  set explicitly once worker count × pool size approaches PgBouncer's budget.
- Redis backs OTP, locks, rate limits (HTTP + WS), the denylist/ban flags, and chat
  pub/sub. All uses are O(1) commands; chat fan-out is per-conversation channels.
  No concern at target scale; chat pub/sub is the first candidate for its own
  instance if Redis CPU ever climbs.
- The app shutdown hook uses FastAPI's `on_event` (deprecated in favour of lifespan
  but fully functional); migrate to a lifespan context manager whenever the factory
  is next reworked.
