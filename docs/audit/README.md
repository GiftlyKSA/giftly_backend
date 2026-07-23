# SAFE-GIFT backend — code audit (current state, 2026-07-21)

A living self-audit of the completed Phase 1–14 codebase. This document tracks the
**currently open** findings and the deliberately accepted trade-offs. The initial audit
(commit `b952838`) raised 17 findings; a remediation pass (commit `ceb44ab`) closed all
17 — that history lives in git, and is not re-listed here. This revision is a fresh
full pass over the remediated tree, and records what a clean read of the *current* code
turns up.

## Scope and method

- **Read in full**: `core/` (money, pricing, crypto, security, jwt, locks, db, config,
  middleware, ratelimit, deps), the money/payment/auth/fulfillment services, the webhook
  route, the admin session/CSRF wiring, the media service, the chat WebSocket, all
  scheduled workers, and the real integration clients — with fresh attention to the code
  the remediation pass changed.
- **Swept by pattern**: raw/f-string SQL, `float()` in money paths, FastAPI imports in
  services (layering), ownership-in-query usage, pagination caps, index coverage,
  cookie flags, secret handling, dev/fake gating, and dead/half-wired feature surface.
- Backed by the live gates: ruff + ruff format, mypy `--strict`, and the test suite at
  ~88 % coverage (all green).

## Files

| File | Covers |
| --- | --- |
| [01-security.md](01-security.md) | AuthN/AuthZ, crypto, webhooks, rate limiting, admin surface, OWASP notes |
| [02-money-integrity.md](02-money-integrity.md) | Ledger, pricing, escrow, holds, reconciliation, payout exit |
| [03-logic-and-correctness.md](03-logic-and-correctness.md) | State machines, races, workers, chat/WS, feature completeness |
| [04-performance.md](04-performance.md) | HTTP clients, middleware stack, query scaling, unbounded growth |
| [05-guidelines-compliance.md](05-guidelines-compliance.md) | CLAUDE.md hard rules, rule by rule |

## Severity legend

- **High** — exploitable or money-affecting; fix before production traffic.
- **Medium** — real weakness or scaling/completeness defect; fix soon, not an emergency.
- **Low** — defence-in-depth gap or papercut; batch into normal work.
- **Info** — deliberate, documented trade-off worth re-confirming periodically.

## Open findings

| ID | Sev | Summary |
| --- | --- | --- |
| NF-1 | Medium | No proxy/forwarded-header handling — peer IP powers the webhook allowlist, rate limiting, and audit IPs |
| NF-2 | Medium | Withdrawals are half-wired: funds enter courier wallets but no code path pays them out |
| NF-3 | Low | Each inbound WS frame costs two Redis round trips (ban check + throttle) |
| NF-4 | Low | Live-token ban revocation depends on a best-effort Redis flag write |
| NF-5 | Low | App shutdown uses the deprecated `on_event` instead of a lifespan handler |
| NF-6 | Info | One cross-test flake (`test_create_rejects_second_active_invoice`) under full-suite runs |

## Accepted trade-offs (re-confirmed this pass)

| ID | Summary |
| --- | --- |
| AT-1 | API rate limiter fails **open** on a Redis error (the OTP limiter fails closed) |
| AT-2 | WebSocket access token travels in the `?token=` query string |
| AT-3 | `statement_cache_size=0` for PgBouncer transaction-mode safety |
| AT-4 | `/api/health/ready` is exempt from throttling by design (keep it behind the LB) |
| AT-5 | Services/repositories are constructed per request (session-scoped correctness) |
| AT-6 | `admin_sessions` / `audit_log` are keep-forever (audit trail) |
| AT-7 | Real push client token-list chunking is unverifiable until FCM credentials land |

None of the open findings is a running-code bug. NF-1 and NF-2 are the two that warrant
a decision: NF-1 is closed by a one-line deploy flag (documented in the README and under
NF-1), and NF-2 is a product-scope call — implement the payout flow, or explicitly defer
it and guard the now-dead `MIN_WITHDRAWAL_AMOUNT`/withdrawal surface.
