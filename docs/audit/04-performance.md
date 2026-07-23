# Performance audit (current state, 2026-07-21)

Context: a city-scoped gifting marketplace — hundreds of requests/second is the realistic
ceiling, not tens of thousands. The graded scaling defects from the first pass are fixed;
what remains here is one deprecation and the accepted trade-offs.

## What holds up well

- **Index coverage matches the query surface**: composite indexes on
  `(delivery_city, status)` and `(customer_id, created_at DESC)`, a GiST index on
  `delivery_location`, partial indexes on nullable uniques, and worker-sweep indexes. The
  sweepers are index-backed with LIMIT batches, not scans.
- **Pagination is capped everywhere**: every list route validates `limit` with
  `Query(ge=1, le=100)` and uses keyset cursors, not OFFSET.
- **The event loop is respected**: every real integration is async httpx or aioboto3, and
  each real client now holds **one pooled `httpx.AsyncClient`** (connection + TLS reuse),
  closed on shutdown — no handshake per gateway charge/SMS/push/email.
- **Reconciliation is O(1) queries**: two SQL aggregates instead of a per-wallet SUM loop
  and an in-memory materialisation of the whole correlation map.
- **Throttling is one round trip**: the rate-limit window is a single Lua eval
  (INCR + EXPIRE + ceiling), for both the HTTP and WS limiters.
- **Hot money path is lean**: `post_group` is one SELECT FOR UPDATE + N inserts + one
  flush, inside a Postgres SAVEPOINT (a near-free nested marker).
- **Unbounded growth is bounded**: `run_purge_refresh_tokens` (nightly, Redis-locked)
  deletes tokens expired past `REFRESH_TOKEN_RETENTION_DAYS`.

## Open findings

### NF-5 (Low) — Shutdown uses the deprecated `on_event`

`_install_shutdown` (`app/main.py`) registers cleanup via `@app.on_event("shutdown")`,
which FastAPI has deprecated in favour of a lifespan context manager (the deprecation
warning shows in the test run). It works correctly today — pooled HTTP clients, Redis, and
the engine are closed — but it should migrate to `lifespan=` the next time the factory is
reworked, before a future Starlette drops `on_event` entirely.

## Accepted trade-offs (re-confirmed)

- **AT-5** — services/repositories are constructed per request; they are thin objects and
  the pattern buys session-scoped correctness. Do not "optimise" into shared-session bugs.
- **AT-6** — `admin_sessions` and `audit_log` are keep-forever (audit trail); give them an
  explicit retention policy when storage, not correctness, makes it worthwhile.
- **AT-7** — the real push client hands its full token list to one `send_push`; whether it
  must chunk to the provider's per-request cap (FCM: 500) can't be verified until real
  credentials land.

## Capacity notes

- Gunicorn + Uvicorn workers, async pool with `pool_pre_ping`: standard and sound. Pool
  sizing is SQLAlchemy defaults (5 + 10 overflow per worker) — set explicitly once
  worker-count × pool-size approaches PgBouncer's budget.
- Redis backs OTP, locks, both rate limiters, the denylist/ban flags, and chat pub/sub —
  all O(1) commands. It is also now a hard dependency of the authenticated request path
  (the ban/denylist MGET); chat pub/sub is the first candidate for its own instance if
  Redis CPU ever climbs.
- NF-1 (proxy IPs, see security) also has a throughput dimension: without trusted
  forwarded headers, per-IP throttling can't distinguish clients behind the LB.
