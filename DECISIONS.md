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
- **2026-07-18** — Invoices (Phase 7): the courier authors items + a net courier fee and,
  optionally, the customer's promo code; the platform computes every other leg through the
  single pricing engine (`core/pricing.py`). The golden worked example (655.50) is the
  regression anchor. Because the `enforce_invoice_item_freeze` trigger forbids item writes
  once an invoice leaves DRAFT, an invoice is created DRAFT, lined, has its promo reserved,
  then transitioned to ISSUED in one transaction; the order moves `ASSIGNED ->
  WAITING_PAYMENT`. `PAYMENT_EXPIRY_HOURS` sets the ISSUED `expires_at`.
- **2026-07-18** — Invoices are immutable once issued: applying a different promo is not an
  edit. A courier `POST /api/invoices/{id}/cancel` releases the promo and reopens the order
  (`WAITING_PAYMENT -> ASSIGNED`) so a corrected invoice is a NEW row, never a mutation.
  The partial unique index `uq_invoices_one_active_per_order` keeps at most one active
  invoice per order, so a second create returns 409 CONFLICT.
- **2026-07-18** — A promo supplied at invoice creation is validated and reserved against
  the **customer** (`order.customer_id`), not the courier — the per-user cap tracks the
  beneficiary. A code that rounds to a 0.00 discount is rejected (422), never stored,
  keeping the `chk_invoice_promo_pairing` invariant (promo_id set <=> discount > 0).
- **2026-07-18** — `POST /api/promos/validate {code, order_id}` (deferred from Phase 5) is
  now delivered as a pure customer preview: it re-runs the pricing engine over the order's
  active-invoice lines with the candidate promo and returns the discount + resulting total,
  reserving nothing. `core.promo_service.to_pricing_promo` was made public so the invoice
  pipeline and the preview build the exact promo value object the promo engine validated.
- **2026-07-18** — Invoice ownership is enforced in the query, consistent with orders: a
  non-participant courier gets 404 on create (via `get_for_actor`), and cancel uses a
  courier-scoped `SELECT ... FOR UPDATE` (`lock_for_courier`) so a non-issuer gets 404
  (no existence leak) rather than 403.
- **2026-07-18** — Payments (Phase 8): a paid invoice's total moves into `SYSTEM_ESCROW`,
  sourced from the customer's wallet and the gateway (`-wallet -gateway +total == 0`), held
  there until delivery/approval (Phase 10). The promo subsidy is injected at payout, not at
  payment, so escrow holds exactly what the customer paid — this keeps the payment group
  balanced without involving `SYSTEM_REVENUE`.
- **2026-07-18** — Split invoice payments HOLD the wallet portion (`held_balance`, a
  reservation, not a ledger movement) before creating the gateway charge, so the same
  balance cannot back two pending payments. On the first successful webhook settle the hold
  is released and the balance is debited; a replay never re-releases it. A wallet that fully
  covers the total settles synchronously with no gateway round-trip.
- **2026-07-18** — Webhook idempotency is enforced at three layers: a Redis lock on the
  transaction number (serializes concurrent duplicate deliveries), the intent's own status
  check (PAID => no-op), and the ledger idempotency key `invoice:<id>:escrow` /
  `intent:<id>:topup` (the race backstop via `uq_transactions_idempotency`). The signature
  is verified over the RAW body before the payload is parsed — never re-serialized.
- **2026-07-18** — The webhook runs in its own committing session (not the request-scoped
  `get_db`) so a settlement is explicit and a gateway retry sees the committed result. The
  fake gateway now issues transaction numbers with a random per-instance prefix so they stay
  globally unique against the `uq_payment_intents_paylink_txn` index across app instances.
- **2026-07-18** — `POST /api/dev/paylink/simulate` (development only) builds the exact
  webhook body for a transaction number, signs it with the fake gateway's test secret, and
  calls the REAL webhook handler — it never bypasses webhook processing (SPEC SECTION 5.1).
- **2026-07-18** — Receipt (Phase 9): the invoice-paid receipt is delivered by a sweeper
  over the `idx_invoices_receipt_pending` index (PAID + `receipt_email_sent_at IS NULL`),
  not by an enqueue on the payment path. The index-backed sweep is the mechanism, so a
  receipt survives a failed send — it stays pending until a later pass delivers it. A
  scheduled TaskIQ task (`deliver_pending_receipts`, every 5 min, Redis-locked) runs it.
- **2026-07-18** — Receipt delivery is at-most-once per sweep (the invoice row is locked
  `FOR UPDATE` and re-checked) and at-least-once overall (send-then-stamp; a crash before
  the commit retries). For a receipt, a rare duplicate is friendlier than a lost email.
  A customer with no email on file has their invoice stamped anyway so the sweeper stops
  retrying it — there is no address to send to.
- **2026-07-18** — Receipt template variables carry amounts and the order/invoice
  reference only — never a phone, coordinates, or any Restricted data (SPEC SECTION 5.3).
  The email client stays behind the `EmailClient` ABC; the sweeper builds the environment's
  client (Fake outside production) or accepts an injected one for tests.
- **2026-07-19** — Fulfilment (Phase 10): the escrow release on completion is one balanced
  group `-total + courier_payout + tax + revenue == 0`, matching the golden approval-payout
  (courier 540.00, tax 85.50, revenue 30.00 for the 655.50 invoice). Keyed on
  `order:<id>:release`, so a customer approval racing the 72h auto-approve job pays out
  exactly once. `core.pricing.compute_settlement` is the single place the split is computed;
  the courier is paid on the PRE-discount base minus commission (ADR 0005), and
  SYSTEM_REVENUE (which may go negative) funds the promo.
- **2026-07-19** — Delivery is geofenced: the courier's submitted position must be within
  `MAX_DELIVERY_RADIUS_METERS` of the drop-off, computed with `ST_Distance` on `geography`
  (metres) — on `geometry` it returns degrees and every check would silently pass. The
  capture location is stored on each DELIVERY_PROOF photo (the `chk_proof_has_location`
  CHECK requires it), built longitude-FIRST.
- **2026-07-19** — Dispute resolution is a JWT ADMIN-role JSON action
  (`POST /api/admin/disputes/{id}/resolve`), NOT part of the cookie-authenticated HTML admin
  dashboard, which stays read-only for money (per its own note). RESOLVED_CUSTOMER refunds
  the full total (→ REFUNDED); RESOLVED_COURIER runs the normal payout (→ COMPLETED);
  RESOLVED_SPLIT divides the escrow between the parties by an admin-supplied courier amount
  and the platform books nothing (→ COMPLETED).
- **2026-07-19** — Ratings derive the rated user from the order (the other participant),
  never from the request body, and are allowed only on a COMPLETED order, one per rater
  (`uq_ratings_order_rater`, `chk_no_self_rating`). Auto-approve and the receipt/reconcile
  jobs share the same sweeper shape: injectable factory + own-engine fallback, per-row
  transactions, and a Redis-locked scheduled entry point.
- **2026-07-19** — Chat (Phase 11): message content and the inbox preview are both
  AES-256-GCM encrypted at rest, bound by AAD to their row/column
  (`build_aad("messages","content",<conv_id>)` and
  `build_aad("conversations","last_message_preview",<conv_id>)`). Plaintext never lands in
  an unencrypted column (ADR 0004); the inbox decrypts one preview per row. Chat search
  stays out of scope because it conflicts with encryption.
- **2026-07-19** — Live delivery uses Redis pub/sub (channel `chat:conversation:<id>`): on
  send the service publishes the decrypted message, and each WebSocket subscribes to its
  conversation's channel and forwards to its socket. This makes delivery work across
  instances without an in-process registry — a message sent on any node reaches every open
  socket. The WS is authenticated by an access token in the `?token=` query param (same
  decode + denylist as REST) and only the two members may connect.
- **2026-07-19** — The WS handler runs two coroutines: a pump forwarding pubsub->socket and
  a loop reading socket->send_message (so clients can send over the socket too). On close
  the reader task is cancelled; because `asyncio.CancelledError` is not an `Exception`
  subclass, cleanup explicitly suppresses it so a normal disconnect never surfaces as an
  error. Unread counters live on the conversation (customer/courier); a send bumps the
  recipient's, mark-read zeroes the reader's and flips `is_read` on inbound messages (the
  only mutation the append-only trigger allows).
- **2026-07-19** — Notifications (Phase 13): push is BEST-EFFORT (failures are logged and
  swallowed) and the body never carries Restricted data — a new-message push says only
  "New message", never the text. Device tokens are unique across users, so registering a
  handed-down device re-points it to the new owner. Notifications are wired at the ROUTER
  boundary (create-order broadcast to city couriers, chat-message push to the recipient) so
  the money-critical services keep pure signatures and their tests untouched.
- **2026-07-19** — Expiry sweeper (`run_expire_stale`, 10 min, Redis-locked): an ISSUED
  invoice past its window is EXPIRED — its held wallet funds are released, its open gateway
  intent is EXPIRED, its promo reservation is returned, and the order reopens to ASSIGNED so
  the courier can re-issue. Driving order-payment expiry off the INVOICE (not the intent)
  avoids the race where a late webhook settles an invoice the sweeper already reopened.
  Wallet-top-up intents expire independently (no money was held).
- **2026-07-19** — Key rotation (`app.rotate_keys`): only the MUTABLE Restricted columns are
  re-encrypted to the active version (courier identity ids, the chat preview, IBANs), each
  under its original AAD. `messages.content` is append-only (the `enforce_message_append_only`
  trigger forbids updating content), so messages are never rotated in place and old key
  versions must remain in `FIELD_ENCRYPTION_KEYS` while any message references them. A blob's
  version is read from its first byte (`crypto.blob_version`) so already-active values are
  skipped and rotation is idempotent.
- **2026-07-19** — Hardening (Phase 14): a global fixed-window rate limiter (default
  120 req / 60 s) throttles every request, keyed by the authenticated user id when the
  request is signed and the client IP otherwise. It is deliberately FAIL-OPEN — a Redis
  outage lets requests through (logged) rather than turning a cache blip into a full
  outage — and the health probes are exempt so an orchestrator can always reach them.
  A body-size guard rejects an oversized `Content-Length` up front with 413 before any
  route or DB call; media never transits the API (pre-signed S3), so the cap only bounds
  JSON. The security-header middleware is now the OUTERMOST layer so even a 429 or 413
  rejected upstream is still stamped and de-fingerprinted. The readiness probe went from
  a static stub to real `SELECT 1` + Redis `PING` checks, returning 503 (never 500) when
  a dependency is down. Tests keep the limiter off by default (the suite shares one Redis,
  so unauthenticated requests would collide in one `ip:unknown` bucket) and opt in per
  test.
- **2026-07-20** — Audit-fix batch (every actionable finding from docs/audit/): a ban now
  revokes ALL live refresh tokens and sets a Redis `auth:banned:<user_id>` flag checked
  by `require_auth` on every request (one MGET with the denylist), and both refresh and
  OTP re-login reject BANNED accounts — SEC-1 closed end to end. `PAYLINK_ALLOWED_IPS`
  is now enforced at the webhook (403 before the HMAC gate). The OTP HMAC key chain is
  `OTP_HMAC_KEY -> JWT_SECRET -> IDENTITY_FINGERPRINT_PEPPER` — never a constant. WS
  chat frames are guarded (JSON-only, `WS_MAX_FRAME_BYTES` cap, per-user Redis throttle,
  mid-connection ban closes the socket) and WS sends now fire the same recipient push as
  REST. All four scheduled workers release their locks via the Lua compare-and-delete
  helper. The rate-limit window is one atomic Lua eval (no strandable counter, one round
  trip). Chunked bodies without a Content-Length are rejected 411. `post_group` runs its
  write in a SAVEPOINT so a concurrent idempotency-key race degrades to a graceful no-op;
  an over-release of a hold logs a WARNING before clamping. Real integration clients hold
  ONE pooled httpx client each (closed on app shutdown). Reconciliation is two SQL
  aggregates (GROUP BY + HAVING) instead of an O(wallets) loop and an O(ledger) dict. A
  nightly `run_purge_refresh_tokens` deletes tokens expired past
  `REFRESH_TOKEN_RETENTION_DAYS`. New knobs are env vars with safe defaults (see
  .env.example). Deliberately NOT changed: the fail-open API limiter, the WS query-param
  token, and `statement_cache_size=0` (documented trade-offs, re-confirmed).
