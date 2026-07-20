# Performance audit

Context: a city-scoped gifting marketplace — hundreds of requests/second is the
realistic ceiling, not tens of thousands. Findings are graded against that reality;
nothing here is an emergency, but PERF-1 and PERF-2 will bite first as volume grows.

## What holds up well

- **Index coverage matches the query surface** (`app/models/tables.py`): composite
  indexes on `(delivery_city, status)` for the radar, `(customer_id, created_at DESC)`
  for lists, a GiST index on `delivery_location`, partial indexes on nullable uniques
  (`idempotency_key`, `email`, `promo_id`), and worker-sweep indexes (receipt-pending,
  auto-approve-due, expiry). The sweepers are index-backed with LIMIT batches, not
  table scans.
- **Pagination is capped everywhere it is exposed**: every list route validates
  `limit` with `Query(ge=1, le=100)` (`app/routers/orders.py:140`, `chat.py:67,99`,
  etc.) and uses keyset cursors, not OFFSET.
- **The event loop is respected**: every real integration is async httpx or aioboto3
  (`app/integrations/*/real.py`) — a sweep found no sync SDK call in an async path.
- **Hot money paths are lean**: `post_group` is one SELECT FOR UPDATE + N inserts +
  one flush; webhook settlement touches exactly the rows it settles.

## Findings

### PERF-1 (Medium) — A fresh `httpx.AsyncClient` per outbound call

Every real client builds and tears down an `AsyncClient` inside each call:
`app/integrations/paylink/real.py:51`, `push/real.py:22`, `sms/real.py:22`,
`email/sndr_client.py:45`. That is a new connection pool, TCP handshake, and TLS
negotiation per gateway charge, SMS, push, and email — typically 50–150 ms of avoidable
latency on the *payment* path, and it defeats HTTP/1.1 keep-alive entirely.

**Recommendation**: give each real client one long-lived `AsyncClient` (created in
`__init__`, closed via an `aclose()` hooked to app shutdown), or share one client on
`app.state`. This is the highest-leverage performance fix in the codebase.

### PERF-2 (Medium) — Reconciliation scales with the entire ledger

`MoneyService.reconcile` (`app/services/money_service.py:420-441`):

1. loads **every** wallet, then issues one `SUM` query per wallet (N+1 —
   `wallet_repository.settled_balance` per row);
2. `correlation_settled_sums` materialises a dict of **every correlation group ever
   posted** — unbounded memory, and the nightly runtime grows linearly with ledger
   history forever.

Fine at 10⁴ transactions; a problem at 10⁷. The job holds only a Redis lock, so a slow
run also overlaps its own 600 s TTL (see SEC-5's plain-DEL interaction).

**Recommendation**: replace the per-wallet loop with a single
`GROUP BY wallet_id` aggregate joined against balances, and make the correlation check
a SQL-side `GROUP BY correlation_id HAVING SUM(amount) != 0` that returns only
violators. Both become one query each, O(violations) memory. Optionally add a
watermark (`checked_through` timestamp) so settled history is verified once, not
nightly forever.

### PERF-3 (Low) — `refresh_tokens` grows without bound

Rotation inserts a new row per refresh (`app/services/auth_service.py:235-243`) and
nothing ever deletes used/expired/revoked rows. At one refresh per user per 30 minutes
of active use, this table becomes the largest in the database within months, and the
`token_hash` lookups stay fast (indexed) while backups and vacuums pay the bill.

**Recommendation**: a small nightly sweep — delete rows where
`expires_at < now() - interval '30 days'` — using the existing worker pattern.
(`admin_sessions` and `audit_log` deserve the same retention decision, deliberately.)

### PERF-4 (Low) — Four stacked `BaseHTTPMiddleware` layers

`_install_middleware` (`app/main.py`) registers security-headers, rate-limit, and
body-guard via `@app.middleware("http")` plus the class-based `RequestIdMiddleware` —
all four are Starlette `BaseHTTPMiddleware`, each wrapping the request in an extra
task/anyio scope. Measured overhead is tens of microseconds each; it is not a today
problem, but it is the first thing a profiler will show. The rate-limit and body-guard
checks could fold into one middleware, and pure-ASGI implementations would remove the
task-per-layer cost if p99 latency ever matters.

### PERF-5 (Info) — Rate limiter costs 2 Redis round-trips per request

`INCR` + (first-hit) `EXPIRE`, plus `TTL` when blocked (`app/core/ratelimit.py:55-60`).
A single Lua eval would make it one round trip and simultaneously fix SEC-6's
atomicity gap — one change, two findings.

### PERF-6 (Info) — Per-request service/repository construction

Routers assemble services and repositories per request (e.g.
`build_payment_service`). These are thin dataclass-like objects; construction cost is
negligible and the pattern buys session-scoped correctness. No action — noted so
nobody "optimises" it into shared-session bugs.

## Capacity notes

- Uvicorn workers behind Gunicorn, async DB pool with `pool_pre_ping`: standard and
  sound. Pool sizing is left to SQLAlchemy defaults (5 + 10 overflow per worker) —
  fine to start; set explicitly once worker count × pool size approaches PgBouncer's
  budget.
- Redis is a single point for OTP, locks, rate limits, denylist, and chat pub/sub.
  All uses are O(1) commands; chat fan-out is per-conversation channels. No concern at
  target scale, but chat pub/sub is the first thing to move to its own instance if
  Redis CPU ever climbs.
