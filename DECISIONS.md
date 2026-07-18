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
- **2026-07-17** — Money/ledger (Phase 4): `MoneyService.post_group` is the single
  double-entry primitive. It asserts the legs sum to 0.00 at runtime, locks every
  involved wallet `FOR UPDATE` in ascending id order, applies each leg, and appends one
  SETTLED row per leg. It flushes before returning: a later group's `SELECT ... FOR
  UPDATE` repopulates the wallet rows and would otherwise discard an earlier group's
  still-pending in-memory balance update (found and fixed via the ledger property test).
- **2026-07-17** — Idempotency in the money service is enforced at two layers: an
  application check on the leg idempotency key (fast path) and the DB
  `uq_transactions_idempotency` unique index (the race backstop). Under concurrent
  double-submit, exactly one group commits and the losers get an IntegrityError the
  caller treats as an already-processed replay.
- **2026-07-17** — Reconciliation checks both invariants over SETTLED rows only, so an
  in-flight PENDING escrow hold (a lone leg not yet settled) never trips the
  per-correlation zero-sum check; once the hold settles, its group balances to 0.00.
- **2026-07-17** — Promo engine (Phase 5): `PromoService.reserve` uses the atomic
  conditional UPDATE (§12.3) — a single `UPDATE ... WHERE ... used_count < cap RETURNING
  used_count` — so the "first 20" global cap can never be overshot by concurrent
  requests. The per-user cap is checked in the same transaction after the UPDATE has
  row-locked the promo; over-limit raises and rolls back the increment. Verified by the
  mandated 50-parallel-against-20 concurrency test.
- **2026-07-17** — The pure discount math is exposed as `core.pricing.compute_promo_discount`
  and reused by both the promo engine and (in Phase 7) the invoice pipeline, so the two
  never disagree on a discount.
- **2026-07-17** — `POST /api/promos/validate {code, order_id}` (the HTTP validate
  endpoint) is deferred to Phase 7: the discountable base comes from the order's active
  invoice, which the invoice service creates. The promo service's `validate(code, base,
  user_id)` is the reusable core and is fully tested now; the router wraps it once the
  invoice base exists.
- **2026-07-18** — Orders (Phase 6): the accept race uses BOTH a Redis lock
  (`lock:order_accept:<id>`, SET NX EX + Lua compare-and-delete release) AND a
  `SELECT ... FOR UPDATE` on the order row. If Redis ever fails open, the DB still
  serializes the assignment; both layers are deliberate (SPEC SECTION 20.C).
- **2026-07-18** — Storage is behind a `StorageClient` ABC with a FakeStorageClient so
  the whole media + order flow runs with no S3: requesting an upload URL registers the
  key in the fake, mirroring a completed client PUT. Magic-byte verification is real in
  the S3 client and reported valid in the fake (no real bytes to inspect).
- **2026-07-18** — A courier gets 404 (not 403) on an order they have no relationship
  to, and the radar summary carries no coordinates; the exact delivery point appears
  only after assignment (SPEC SECTION 17.3, A01). Media/upload input errors are 400
  (`BAD_REQUEST`), distinct from 422 semantic errors.
- **2026-07-18** — Order broadcast to city couriers is deferred to Phase 13
  (notifications); the radar (`GET /api/orders/available`) already lets couriers find
  new orders without push, so the flow is complete without it.
