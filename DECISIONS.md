# DECISIONS

Rule-2 judgment calls (where the spec left a detail unstated, the most secure /
explicit / consistent option was chosen) and environment-forced deviations, dated.

- **2026-07-17** — `requires-python = ">=3.11"` instead of the mandated 3.13+. The build
  environment cannot download a 3.13 interpreter (egress policy blocks
  python-build-standalone with a 403), so the floor is relaxed to the available 3.11 to
  keep the stack runnable and green. All code is written to run on 3.11+; the Dockerfile
  still targets `python:3.13-slim`. Revert the floor to `>=3.13` once a 3.13 interpreter
  is available.
- **2026-07-17** — The pricing engine takes a small frozen `PricingConfig` value object
  rather than the whole `Settings`. This keeps `core/pricing.py` pure and trivially
  testable (no settings construction in unit tests) while callers pass the exact rates
  from settings. Same authority, narrower surface.
- **2026-07-17** — Native PG enum types are created explicitly in the baseline migration
  (guarded `DO $$ ... duplicate_object ...` blocks) and every ORM `ENUM` uses
  `create_type=False`. This gives deterministic creation order and idempotency instead
  of SQLAlchemy attempting per-column `CREATE TYPE`.
- **2026-07-17** — GeoAlchemy2's automatic spatial index is disabled
  (`spatial_index=False`) on both geometry columns; the single explicit
  `idx_orders_location_gist` is the GIST index the spec lists, and proof-media capture
  points get no spatial index (none is in the spec's index list). Avoids a duplicate
  index on `orders`.
- **2026-07-17** — `audit_logs.metadata` is mapped in Python as `audit_metadata`
  (column name still `metadata`) because `metadata` is reserved on the SQLAlchemy
  declarative base.
- **2026-07-17** — The invoice-item freeze trigger returns early (allows the operation)
  when the parent invoice row no longer exists, so a legitimate `ON DELETE CASCADE`
  from a (policy-forbidden but structurally possible) invoice delete is not blocked by
  the freeze check. Editing a live non-DRAFT invoice's items is still rejected.
- **2026-07-17** — Development CORS uses `allow_origins=["*"]` with
  `allow_credentials=False` (never wildcard origins with credentials). Production uses
  the exact allow-list with credentials enabled.
- **2026-07-17** — Admin dashboard (Phase 12): built the auth/session/CSRF/step-up
  infrastructure and all read pages plus the non-money mutations (courier verify,
  identity/IBAN reveal, promo create/activate/deactivate, user ban/unban), each
  audited. Money-moving admin actions (dispute payout resolution, withdrawal
  settlement) are shown read-only because they must run through the double-entry
  ledger service (Phase 4), which is not built yet — rather than duplicate money logic
  in the dashboard (which §18.1 forbids), those pages defer to the ledger service.
- **2026-07-17** — The admin CSRF token is a per-session HMAC of the session-token
  hash keyed with `ADMIN_SESSION_SECRET`, so it needs no server-side storage and
  rotates with the session. Login POSTs (pre-session) rely on `SameSite=Strict` plus
  the OTP secret; post-auth mutations require the CSRF token.
- **2026-07-17** — The OTP code is stored in Redis as an HMAC keyed with `JWT_SECRET`
  (a validated >=32-byte app secret). The code is ephemeral (180s), and the HMAC
  protects a Redis dump; a dedicated OTP pepper was judged unnecessary given the TTL.
- **2026-07-17** — The shared async engine, session factory, and Redis client are
  built once in `create_app` and held on `app.state`; admin request handlers open a
  per-request session that commits on success and rolls back on error.
- **2026-07-17** — Auth (Phase 3): added a `refresh_tokens` table (not in SPEC SECTION
  10). The spec mandates rotating refresh tokens with families and reuse detection but
  defines no storage; a dedicated table storing only the SHA-256 hash, family id, and
  used/revoked timestamps is the most explicit and auditable option. Migration
  `dadcdbda923c` adds it (reversible; validated up+down).
- **2026-07-17** — Refresh-token reuse detection must persist even though the request
  returns 401: the auth service commits the family revoke explicitly before raising,
  because the per-request session otherwise rolls back on the exception. This is the one
  place a security side-effect is committed inside a service.
- **2026-07-17** — A new phone that verifies an OTP receives a short-lived registration
  token (10 min, `purpose=registration`), not an access token; `/api/auth/register`
  consumes it. The JWT decoder rejects a registration token where an access token is
  expected and vice-versa, so the two token types are not interchangeable.
- **2026-07-17** — `email` is validated with a constrained-string pattern rather than
  Pydantic `EmailStr` to avoid adding the `email-validator` dependency for a field used
  only for the invoice-paid receipt.
