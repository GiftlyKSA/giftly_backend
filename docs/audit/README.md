# SAFE-GIFT backend — full code audit (2026-08-01)

This folder is a fresh audit of the current tree after remediation. It replaces the
2026-07-21 reports. Findings were verified against code, migrations, generated OpenAPI,
and real PostgreSQL/PostGIS + Redis tests; historical findings are not carried forward
unless they still apply.

## Scope and evidence

- Read and traced every module under `app/core`, every service/repository/router, all
  workers and real integration adapters, ORM constraints, Alembic revisions, Docker
  configuration, CI, and the public docs.
- Swept for unsafe SQL construction, money floats/quantization, missing ownership
  filters, secret leakage, broad exception handling, unbounded queries, dead config,
  state transitions, idempotency, external effects before commit, and fake-production
  interlock gaps.
- Gates: Ruff and format clean; strict mypy clean; Bandit clean; `pip-audit --strict`
  reports no known vulnerabilities; Alembic reports no application-model drift;
  Docker Compose config validates.
- 233 tests passed in the full four-worker run. Coverage measured **87.90%**, above the
  85% CI gate.

## Remediation completed in this pass

| Area | Result |
| --- | --- |
| Trusted proxy IPs | `FORWARDED_ALLOW_IPS` is explicit in env/Compose; server-level trust remains CIDR-scoped. |
| Withdrawals | Complete request → approve/reject → paid flow; encrypted Saudi IBAN, held funds, row locks, audit rows, idempotency, and balanced ledger settlement. |
| WebSocket guards | Ban check and rate limit now share one Redis Lua round trip and fail closed. |
| Application lifecycle | Deprecated shutdown event replaced with FastAPI lifespan cleanup. |
| Admin authentication | Environment-backed credentials, fail-closed attempt throttling, secure sessions, password step-up, and production rejection of development defaults. |
| Chat durability | Message commits and recipient notification finish before Redis publishes the live event. |
| Private media | Upload URL signs exact length and content type; CloudFront read URLs are RSA/SHA-256 signed; production storage config is fail-closed. |
| Push fanout | Provider calls are bounded to 500 tokens per batch. |
| Runtime guards | Optimization-sensitive asserts replaced with explicit errors. |
| Migration checks | Extension-owned PostGIS tables no longer pollute Alembic drift checks. |

## Open findings

| ID | Severity | Summary |
| --- | --- | --- |
| OPEN-1 | High | Paylink, sndr, SMS, and push adapters still contain vendor-contract placeholders and need sandbox/live contract tests before production. |
| OPEN-2 | Medium | Private media read signing exists, but no ownership-checked endpoint/response currently delivers signed read URLs to clients. |
| OPEN-3 | Medium | `held_balance` mutations are locked, but reconciliation does not reconstruct holds from pending invoices and withdrawals. |
| OPEN-4 | Low | Key rotation and wallet reconciliation materialize full tables; acceptable now, but should page/stream before large scale. |
| OPEN-5 | Low | Live access-token ban revocation is Redis-backed; Redis data loss can restore a banned token until its ≤30-minute JWT expiry. |
| OPEN-6 | Info | Starlette emits a TestClient/httpx deprecation warning; application lifespan usage itself is current. |

## Accepted trade-offs

- The ordinary HTTP rate limiter fails open on Redis failure; OTP, admin authentication,
  and guarded WebSocket paths fail closed.
- Browser WebSocket authentication uses `?token=`; proxy/access logs must redact query
  strings until single-use WS tickets are introduced.
- `statement_cache_size=0` remains enabled for PgBouncer transaction-mode safety.
- Health probes are not throttled and must stay behind the load balancer.
- Admin/audit history has no automatic retention policy.

OPEN-1 and OPEN-2 are production launch blockers. OPEN-3 should be resolved before
withdrawal/payment volume makes manual hold investigation impractical.
