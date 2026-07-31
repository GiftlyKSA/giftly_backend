# Performance audit (2026-07-31)

## Verdict

The online request paths are appropriate for the target scale: async I/O, pooled vendor
clients, bounded request bodies and pages, indexed keyset reads, atomic Redis scripts,
and batch-limited workers. No N+1 defect was found on a money or list path.

## Verified strengths

- Real HTTP integrations reuse one `httpx.AsyncClient` and close it through lifespan.
- PostgreSQL writes use short transactions and deterministic wallet lock order.
- Reconciliation uses aggregate SQL rather than one query per wallet/correlation.
- HTTP throttling is one Lua call. WebSocket ban + throttle is also one Lua call.
- List endpoints cap page size at 100 and use keyset cursors; worker sweeps use indexed
  limits. Refresh-token cleanup bounds auth-table growth.
- Push delivery splits token lists into provider-safe batches of 500.
- S3 bytes bypass the API; upload and JSON body sizes are independently bounded.

## Finding

### OPEN-4 (Low) — maintenance paths materialize full tables

`KeyRotationService` loads all courier profiles, conversations, and withdrawals for a
rotation. Reconciliation aggregates transactions efficiently but still materializes all
wallet rows to compare balances. These are offline/nightly paths and acceptable at the
current scale, but memory grows linearly. Move them to primary-key pages or server-side
streaming before tables reach hundreds of thousands of rows.

## Capacity and test notes

- SQLAlchemy defaults imply up to 15 connections per process (5 pool + 10 overflow).
  Set explicit pool budgets when worker count and PgBouncer capacity are known.
- Redis is shared by OTP, auth revocation, locks, throttles, TaskIQ, and pub/sub. Split
  chat/broker traffic first if latency or eviction pressure appears.
- The complete integration suite exceeds five minutes sequentially on Windows Docker
  Desktop because tests intentionally create/dispose isolated engines. CI/Linux remains
  the authoritative wall-clock environment; bounded local groups all pass.
- Starlette's current TestClient/httpx compatibility shim emits a dependency deprecation
  warning (OPEN-6). It does not affect production request handling.
