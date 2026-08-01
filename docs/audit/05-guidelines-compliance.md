# Guidelines compliance audit (2026-08-01)

`CLAUDE.md` was checked rule by rule against the remediated tree.

| Rule | Verdict | Evidence |
| --- | --- | --- |
| `uv` only | Pass | `pyproject.toml`/`uv.lock`, Docker and CI use uv; no alternate package manifest. |
| Money is Decimal, never float | Pass | Money schemas parse strings; float conversions are geospatial only. |
| Money quantization only in `core/money.py` | Pass | Source sweep finds money `.quantize` only in that module. |
| Pricing only in `core/pricing.py` | Pass | Invoice/promo services call the shared engine. |
| No unsafe/raw dynamic SQL | Pass | SQLAlchemy expressions and bound `text()` parameters only; no f-string SQL. |
| Ownership enforced in queries | Pass | Actor IDs originate in verified JWTs and scope repository reads. |
| Append-only ledger | Pass | Trigger-enforced; all corrections are balanced new groups. |
| Secrets from env and wrapped | Pass | Secret settings use `SecretStr`; image/config scan is clean. |
| No healthcare data | Pass | No healthcare fields or inferred health attributes. |
| `/api` without version prefix | Pass | Router and OpenAPI sweep clean. |
| Layering | Pass | Routers wire services/repositories; services do not import FastAPI. |
| PostGIS coordinate/distance rules | Pass | `ST_MakePoint(lng, lat)` and geography distances at every call site. |
| Redis lock token release | Pass | Shared Lua compare-and-delete helper used by workers and money locks. |
| Async-safe external I/O | Pass | Async httpx/aioboto3 only; pooled clients close in lifespan. |
| Raw-body webhook verification | Pass | HMAC receives `request.body()` bytes unchanged. |
| Conventional commits | Pass | Reviewed recent history; final change uses a conventional commit. |
| Google docstrings and formatting | Pass | Ruff and format checks clean. |
| Docs follow contract changes | Pass | Endpoint docs and generated OpenAPI include withdrawals and headers. |
| Admin table access boundary | Pass | Metadata-backed catalog is read-only by default; audited CRUD exists only for users, courier profiles, and safe pre-payment orders. |
| Tests for behavior changes | Pass | 233 tests, 87.90% coverage; race, storage, withdrawal, push, guard, and admin credential tests. |
| No committed secrets or `.env` | Pass | Git ignore plus history/diff review; only explicit development/test values. |
| Naming conventions | Pass | Plural tables, UUID PKs, money/encrypted suffixes, indexed migration. |
| Production fake interlock | Pass | Boot validation plus fake constructors fail closed. |

## Configuration hygiene

- The previously unused `MAX_WITHDRAWAL_AMOUNT` environment setting is now modeled,
  validated, and enforced. The dead `FCM_CREDENTIALS` setting was removed because the
  selected push adapter uses Supabase credentials.
- Production now refuses missing Paylink, sndr, SMS, push, S3, or CloudFront settings.
- `FORWARDED_ALLOW_IPS` is documented and present in local Compose. Deployments must use
  exact proxy IPs/CIDRs and must never set a public listener to trust `*`.
- Alembic autogeneration ignores extension-owned tables but compares every application
  table; `alembic check` reports no application upgrade operations.

## Overall result

All repository hard rules pass. The open items in the audit README are product/vendor
readiness or scale risks, not rule violations. OPEN-1 and OPEN-2 must still block a
production launch despite the code-quality gates being green.
