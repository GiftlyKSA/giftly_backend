# Logic and correctness audit (2026-08-01)

## Verdict

State machines, ownership, concurrency, workers, and internal integration boundaries are
coherent. This pass found and fixed one real race: chat published before commit. Two
feature-completeness gaps remain at external/media boundaries.

## Verified behavior

- Order transitions use one state machine. Payment, invoice, escrow, delivery, approval,
  dispute, and expiry mutations share the request transaction.
- Courier accept, promo reservation, webhooks, settlements, and withdrawals combine
  row/Redis locks with database predicates or unique keys; correctness does not depend
  on the cache lock alone.
- Scheduled jobs use token-owned Lua lock release and per-row transactions. Receipt
  send/stamp semantics remain retry-safe for the available provider contract.
- Longitude/latitude order and geography casts are correct at all PostGIS call sites.
- WebSocket membership precedes accept. Frames are bounded and JSON-only; ban plus
  throttle is one fail-closed Redis script. Messages now commit, notify, then publish,
  so a received live event is immediately readable from durable history.
- FastAPI lifespan closes HTTP clients, Redis, and SQLAlchemy pools without deprecated
  application shutdown events.
- The previously reported invoice cross-test flake was not reproduced. Its fixtures use
  unique IDs and outer transaction rollback. The actual flaky behavior was the chat
  publish/commit race and now has a passing regression test.

## Findings

### OPEN-1 (High) — vendor adapters are placeholders until contract-tested

The StreamPay adapter follows the vendor's published OpenAPI contract, but it has not yet
been exercised against a merchant sandbox. This is not safe to discover with real customer
payments or OTP traffic. Treat sandbox contract tests as a release requirement.

### OPEN-2 (Medium) — private media has no authorized read delivery

Upload and confirmation work, and signing is implemented, but API responses expose only
storage keys. Clients cannot display a private object without a signed URL. Add URLs only
where an existing order/conversation query proves access; never add a generic key signer.

## Test evidence

- 233 tests collected.
- 87 unit tests pass.
- All 146 integration tests pass against PostgreSQL 16/PostGIS and password-protected
  Redis.
- The complete 233-test, four-worker run passes with 87.90% coverage.
