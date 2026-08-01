# SAFE-GIFT Backend

SAFE-GIFT is a two-sided mobile marketplace for **custom** gifting: a customer posts a
gift request tied to a city and a delivery date, a verified courier claims it and
builds an itemised invoice, the customer pays into platform escrow, and funds release
to the courier only after geofenced, photo-proven delivery is approved. This repository
is the backend, admin dashboard, and documentation — there is no mobile/web client here.

> Build status: **all 14 phases implemented and green.** Configuration and the
> production safety interlock; the money/pricing/crypto engines; the full data layer
> (23 application tables, constraints, indexes, ledger/immutability triggers, system-wallet seed);
> the OTP→JWT auth API with refresh rotation; the double-entry ledger and wallet
> endpoints; the promo engine with atomic reservation; orders with the courier radar
> and geofenced delivery; itemised invoices and pricing; split wallet/gateway payments
> into escrow with the signed webhook; the receipt email sweeper; delivery, approval,
> payout, disputes, and ratings; encrypted chat with a live WebSocket; push
> notifications, the expiry sweeper, and encryption-key rotation; hardening (global
> rate limiting, body-size guard, real readiness probe); and the **server-rendered
> admin dashboard**. CI enforces lint, strict types, ≥85 % test coverage, a fresh
> OpenAPI spec, a non-root image, and secret-free image history. A full self-audit
> lives in `docs/audit/`; see `DECISIONS.md` for the decision log.

## Architecture

```
                 +----------------------------------------------+
   Mobile app -->|  routers/ (HTTP + WS)      admin/ (Jinja)     |
                 |        |                          |            |
                 |        v                          v            |
                 |             services/ (all business logic)     |
                 |        |                          |            |
                 |        v                          v            |
                 |   repositories/ (all DB access)   integrations/|--> Paylink / sndr /
                 |        |                                        |    SMS / Push / S3
                 |        v                                        |
                 |   models/ (SQLAlchemy)  <--  core/ (config,     |
                 |                              money, pricing,    |
                 |                              crypto, security)  |
                 +----------------------------------------------+
   PostgreSQL 16 + PostGIS + pgcrypto  .  Redis 7  .  private S3 + CloudFront
```

Dependency rule (strict): `routers/admin -> services -> repositories -> models`, never
the reverse. A module may import another module's **service interface** only.

## Tech stack

Python 3.13 (see the note in `DECISIONS.md`), FastAPI + Uvicorn/Gunicorn, Pydantic v2,
SQLAlchemy 2 async + asyncpg, Alembic, Redis, TaskIQ, PostgreSQL 16 + PostGIS,
`cryptography`, PyJWT, Jinja2 (admin), ruff, mypy, pytest. Package management is **`uv`
only** — `pip`, `poetry`, `pipenv`, `virtualenv`, and `conda` are forbidden everywhere.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Astral)
- Docker + Docker Compose (for the local Postgres/Redis stack)

## Quickstart

```bash
uv sync                                   # install from the committed uv.lock
cp .env.example .env                      # then fill in the blanks (see the table below)
docker compose up -d                      # Postgres (PostGIS) + Redis, bound to 127.0.0.1
uv run alembic upgrade head               # apply the schema (creates system wallets)
uv run python -m app.seed                 # idempotent safety-net seed
uv run uvicorn app.main:create_app --factory --reload   # http://localhost:8000
```

Health check: `curl localhost:8000/api/health`. In development the OpenAPI docs are at
`/docs`; they are disabled in test and production by design.

### Optional: PgBouncer pooling path

```bash
docker compose --profile full up -d       # adds pgbouncer (transaction mode) on :6432
```

## Environment variables

Every secret is an environment variable — there is no secrets manager. Secrets are
wrapped in `SecretStr`, never logged, and never baked into the image; they are injected
at runtime (task definition / compose override / systemd `EnvironmentFile`). See
`.env.example` for the full, always-blank template.

| Name | Required in | Description | Example (never a real value) |
| --- | --- | --- | --- |
| `ENVIRONMENT` | all | `development` \| `test` \| `production`; no default | `development` |
| `DATABASE_URL` | all | async Postgres DSN | `postgresql+asyncpg://user:pass@host/db` |
| `REDIS_URL` | all | Redis DSN | `redis://:pass@host:6379/0` |
| `JWT_SECRET` | all (HS256) | >= 32-byte signing secret | `<32+ random bytes>` |
| `JWT_ALGORITHM` | all | `HS256` or `RS256` (pinned, never from token) | `HS256` |
| `FIELD_ENCRYPTION_KEYS` | all | JSON version->base64 32-byte key map | `{"1":"<base64 32B>"}` |
| `FIELD_ENCRYPTION_KEY_VERSION` | all | active key version in the map | `1` |
| `IDENTITY_FINGERPRINT_PEPPER` | all | >= 32 bytes, distinct from every enc key | `<32+ random bytes>` |
| `CORS_ALLOWED_ORIGINS` | production | exact origins; wildcard banned | `https://app.example.com` |
| `PAYLINK_*` | production | gateway id/secret/webhook/IPs/callback | — |
| `SNDR_*` | production | email api key/base url/from/template | — |
| `AWS_*`, `S3_BUCKET_NAME`, `CLOUDFRONT_*` | production | storage + signed CDN | — |
| `ADMIN_SESSION_SECRET` | if dashboard on | >= 32 bytes | `<32+ random bytes>` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | if dashboard on | environment-backed login; production password >= 12 chars | `admin` / `admin` (development only) |

The application **refuses to boot** if any production-safety rule is violated (DEBUG on,
docs enabled, empty/wildcard CORS, missing Paylink/sndr config, a bad encryption key, or
a pepper equal to an encryption key). This is the first of four layers of the production
safety interlock (SPEC SECTION 5.2).

### How secrets reach the container in deployment

Never via the image. Inject at runtime with your orchestrator's mechanism — an ECS task
definition's `secrets`, a Kubernetes `Secret` mounted as env, a systemd
`EnvironmentFile`, or an uncommitted compose `override`. `docker history` must reveal
nothing.

### Running behind a proxy or load balancer (required for correct client IPs)

The app reads the peer address (`request.client.host`) for three things: the Paylink
webhook source-IP allowlist (`PAYLINK_ALLOWED_IPS`), the per-IP rate-limit bucket for
unauthenticated requests, and admin audit logging. Behind a reverse proxy or load
balancer this peer is the **proxy**, not the real client, unless you make the ASGI
server trust the forwarded header from that proxy:

```bash
# Trust X-Forwarded-For ONLY from your known proxy CIDRs — never "*".
gunicorn app.main:create_app --factory \
  --worker-class uvicorn.workers.UvicornWorker \
  --forwarded-allow-ips="10.0.0.0/8"
```

Without this, `PAYLINK_ALLOWED_IPS` must list the proxy's egress IP (not Paylink's) or
it will reject every webhook, and all unauthenticated traffic collapses into a single
rate-limit bucket. Do not parse `X-Forwarded-For` in application code — trusting a
client-supplied header is a spoofing footgun; let the server strip it against a trusted
proxy list. This is audit finding **NF-1** in `docs/audit/`.

## Running tests, lint, and types

```bash
uv run pytest                 # full suite
uv run pytest tests/unit -q   # fast unit tests only
uvx ruff check --fix          # lint (replaces black, isort, flake8)
uvx ruff format               # format
uv run mypy app               # strict types on services/, repositories/, core/
```

## Migrations

```bash
uv run alembic revision --autogenerate -m "describe change"   # DRAFT — read every line
uv run alembic upgrade head                                   # apply
uv run alembic downgrade -1                                   # roll back one revision
```

Every revision has a descriptive slug, a docstring, and a working `downgrade()`. An
autogenerated migration is a draft: read it before committing (SPEC SECTION 4.8).

## Project layout

```
app/
  core/          config, security, crypto, money, pricing, logging, exceptions, db
  models/        SQLAlchemy ORM + enums + base mixins
  integrations/  paylink/ email/ sms/ push/ storage/ — each behind an ABC, with fakes
  routers/       HTTP + WS (health, dev)              services/ repositories/ (per phase)
  admin/         server-rendered dashboard            workers/  background tasks
  migrations/    Alembic
docs/            written for a separate UI-building agent (see docs/README.md)
tests/           mirrors the source tree
```

## Dev-mode simulate-payment route

In `development` only, `POST /api/dev/paylink/simulate-payment {transaction_no}` fires a
correctly-signed webhook at the **real** webhook handler, so the whole payment flow runs
with zero Paylink credentials. The route is not registered in test or production, and a
test asserts it 404s there.

## Admin dashboard

Server-rendered (Jinja2), mounted at `/admin`, gated by `ADMIN_DASHBOARD_ENABLED`. It
authenticates with environment-backed username/password into server-side sessions and
calls the **same** services as the JSON API — it never queries the DB directly.

`/admin/tables` provides a paginated, redacted view of every application table. Those
views are read-only except for audited, step-up-protected user, courier-profile, and
eligible order delivery-detail updates. See `docs/endpoints/admin.md` for the exact
field-level boundary.

## Docs

`docs/` is a first-class deliverable written for a UI-building agent that cannot read
this source. Start at `docs/README.md`. `docs/openapi.json` is exported by CI.

## Audit

`docs/audit/` holds a full self-audit of the finished codebase (security, money
integrity, logic, performance, and guideline compliance), with severity-ranked
findings and file:line evidence. Start at `docs/audit/README.md` — the finding index
table is the executive summary. **All 17 actionable findings are fixed** (re-audited
2026-07-20); what remains is the documented accepted-trade-off list and one
vendor-blocked item.

## Troubleshooting

- **App refuses to boot naming a variable** — that is the interlock working; fix that
  variable in `.env`.
- **`alembic upgrade` fails on `type "geometry" does not exist`** — use a
  `postgis/postgis` Postgres image; the baseline migration creates the extensions.
- **TLS/proxy errors** — see the environment's proxy notes; never disable TLS
  verification.
