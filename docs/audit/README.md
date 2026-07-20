# SAFE-GIFT backend — full code audit (re-audited 2026-07-20)

A self-audit of the completed Phase 1–14 codebase, first taken at commit `92ee206` and
**re-audited after the audit-fix batch** that followed it. Every finding cites the file
and line it was observed at; every fix was re-verified against the code and covered by
a test where one was practical.

## Scope and method

- **Read in full**: `core/` (money, pricing, crypto, security, jwt, locks, db, config,
  middleware, ratelimit, deps), the money/payment/auth services, the webhook route, the
  admin session/CSRF wiring, the media service, the chat WebSocket, all scheduled
  workers, and the real integration clients.
- **Swept by pattern**: raw/f-string SQL, `float()` in money paths, FastAPI imports in
  services (layering), ownership-in-query usage, pagination caps, index coverage,
  cookie flags, secret handling, and dev/fake gating.
- Backed by the live gates: ruff + ruff format, mypy `--strict`, and the 217-test suite
  at 87.9 % coverage (all green after the fix batch).

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

## Finding index

| ID | Sev | Status | One-line summary |
| --- | --- | --- | --- |
| SEC-1 | High | ✅ Fixed | Ban now revokes refresh tokens, flags Redis, and blocks refresh/re-login |
| SEC-2 | Medium | ✅ Fixed | `PAYLINK_ALLOWED_IPS` is enforced at the webhook (403 before HMAC) |
| SEC-3 | Medium | ✅ Fixed | OTP HMAC chain `OTP_HMAC_KEY → JWT_SECRET → pepper`; never a constant |
| SEC-4 | Medium | ✅ Fixed | WS frames: JSON-only, size-capped, per-user Redis throttle |
| LOG-1 | Medium | ✅ Fixed | WS sends fire the same recipient push as REST |
| PERF-1 | Medium | ✅ Fixed | One pooled `httpx.AsyncClient` per real client, closed on shutdown |
| PERF-2 | Medium | ✅ Fixed | Reconciliation is two SQL aggregates (GROUP BY + HAVING) |
| SEC-5 | Low | ✅ Fixed | All four workers release locks via the Lua compare-and-delete helper |
| SEC-6 | Low | ✅ Fixed | Throttle window is one atomic Lua eval; OTP counters self-repair |
| SEC-7 | Low | ✅ Fixed | Chunked bodies without Content-Length rejected 411 |
| MON-1 | Low | ✅ Fixed | Idempotency-key race degrades to a no-op via SAVEPOINT |
| MON-2 | Low | ✅ Fixed | Hold over-release logs a WARNING before clamping |
| PERF-3 | Low | ✅ Fixed | Nightly `run_purge_refresh_tokens` bounds table growth |
| PERF-4 | Low | ✅ Fixed | Guards merged into one middleware (4 layers → 3) |
| LOG-2 | Low | ✅ Fixed | A mid-connection ban closes the WebSocket |
| LOG-3 | Low | ✅ Fixed | WS frames must be JSON; raw frames are rejected |
| PERF-5 | Info | ✅ Fixed | Rate-limit check is one Redis round trip (came free with SEC-6) |
| SEC-8 | Info | Accepted | API rate limiter is fail-open by design (OTP limiter fails closed) |
| SEC-9 | Info | Accepted | WS access token travels in the query string |
| MON-3 | Info | Accepted | `statement_cache_size=0` trades speed for PgBouncer safety |
| LOG-4 | Info | Accepted | Readiness probe is unthrottled by design; keep it behind the LB |
| LOG-5 | Info | Open | Confirm real push client chunks token lists when FCM credentials land |
| PERF-6 | Info | Accepted | Per-request service construction is deliberate |

**All 17 actionable findings are fixed.** What remains is the accepted-trade-off list
(re-confirmed this pass, documented in DECISIONS.md) and one vendor-blocked item
(LOG-5) that cannot be verified until real push credentials exist. Tunables introduced
by the fixes are env vars with safe defaults — see `.env.example` (`OTP_HMAC_KEY`,
`WS_*`, `RATE_LIMIT_*`, `MAX_REQUEST_BODY_BYTES`, `REFRESH_TOKEN_RETENTION_DAYS`).
