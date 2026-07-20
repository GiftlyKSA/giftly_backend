# SAFE-GIFT backend — full code audit (2026-07-20)

A self-audit of the completed Phase 1–14 codebase at commit `92ee206`, covering logic,
security, performance, and compliance with the project's own working rules (CLAUDE.md
and the master spec). Every finding cites the file and line it was observed at.

## Scope and method

- **Read in full**: `core/` (money, pricing, crypto, security, jwt, locks, db, config,
  middleware, ratelimit, deps), the money/payment/auth services, the webhook route, the
  admin session/CSRF wiring, the media service, the chat WebSocket, all four scheduled
  workers, and the real integration clients.
- **Swept by pattern**: raw/f-string SQL, `float()` in money paths, FastAPI imports in
  services (layering), ownership-in-query usage, pagination caps, index coverage,
  cookie flags, secret handling, and dev/fake gating.
- Backed by the live gates: ruff + ruff format, mypy `--strict`, and the 210-test suite
  at 87.9 % coverage (all green at the audited commit).

## Files

| File | Covers |
| --- | --- |
| [01-security.md](01-security.md) | AuthN/AuthZ, crypto, webhooks, rate limiting, admin surface, OWASP notes |
| [02-money-integrity.md](02-money-integrity.md) | Ledger, pricing, escrow, holds, reconciliation |
| [03-logic-and-correctness.md](03-logic-and-correctness.md) | State machines, races, workers, chat/WS behaviour |
| [04-performance.md](04-performance.md) | HTTP clients, middleware stack, query scaling, unbounded growth |
| [05-guidelines-compliance.md](05-guidelines-compliance.md) | CLAUDE.md hard rules, rule by rule |

## Severity legend

- **High** — exploitable or money-affecting; fix before production traffic.
- **Medium** — real weakness or scaling defect; fix soon, not an emergency.
- **Low** — defence-in-depth gap or papercut; batch into normal work.
- **Info** — deliberate, documented trade-off worth re-confirming periodically.

## Finding index (by severity)

| ID | Sev | One-line summary |
| --- | --- | --- |
| SEC-1 | High | Banning a user does not revoke their live access or refresh tokens |
| SEC-2 | Medium | `PAYLINK_ALLOWED_IPS` is configured and required, but never enforced |
| SEC-3 | Medium | OTP HMAC falls back to the literal key `"otp"` under RS256 config |
| SEC-4 | Medium | WebSocket chat has no rate limiting or inbound backpressure |
| LOG-1 | Medium | Messages sent over the WebSocket never trigger a push notification |
| PERF-1 | Medium | Every outbound integration call builds a fresh `httpx.AsyncClient` |
| PERF-2 | Medium | Reconciliation is O(entire ledger) with an N+1 per-wallet SUM |
| SEC-5 | Low | Scheduled workers release their Redis lock with a plain `DEL` |
| SEC-6 | Low | `INCR`-then-`EXPIRE` throttles can leave a counter with no TTL |
| SEC-7 | Low | Body-size guard trusts `Content-Length`; chunked bodies bypass it |
| MON-1 | Low | Concurrent idempotent posts surface as a 500, not a graceful no-op |
| MON-2 | Low | `release_hold` clamps at zero, masking double-release bugs |
| PERF-3 | Low | `refresh_tokens` grows without bound (no purge job) |
| PERF-4 | Low | Four stacked `BaseHTTPMiddleware` layers add per-request task overhead |
| SEC-8 | Info | Rate limiter is fail-open by design |
| SEC-9 | Info | WS access token travels in the query string |
| MON-3 | Info | `statement_cache_size=0` trades speed for PgBouncer safety |

No High finding touches money movement; SEC-1 is an authorization-lifecycle gap. The
ledger, pricing, and escrow engines came through the audit clean.
