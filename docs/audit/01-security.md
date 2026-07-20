# Security audit (re-audited 2026-07-20 after the fix batch)

## What holds up well

Controls verified as correctly built (unchanged by the fix batch):

- **JWT handling** (`app/core/jwt.py`): the algorithm is pinned from config and never
  read from the token header (defeats `alg:none` and RS256→HS256 confusion); `iss`,
  `aud`, `exp`, and required claims are all enforced; registration tokens carry a
  `purpose` claim and access-token decoding rejects any token that has one
  (`jwt.py:93-94`), so the two token types cannot be swapped.
- **Refresh tokens** (`app/services/auth_service.py`): opaque, stored only as SHA-256,
  rotated on use, family-revoked on reuse — and the family revoke is committed
  explicitly before the 401 propagates so the compromise marker survives the request
  rollback.
- **Field encryption** (`app/core/crypto.py`): AES-256-GCM with a fresh 12-byte CSPRNG
  IV per write, versioned keys, and an AAD binding every ciphertext to
  `table:column:entity_id`. Key rotation respects the append-only messages table.
- **Webhook verification** (`app/integrations/paylink/real.py:60-63`): HMAC-SHA256 over
  the **raw** body with `hmac.compare_digest` — now layered behind the source-IP gate.
- **Admin surface** (`app/admin/deps.py`, `app/admin/router.py`): server-side sessions
  in httponly cookies (`secure` in production, `samesite=strict`, `path=/admin`),
  HMAC-derived stateless CSRF tokens verified in constant time, step-up re-auth, and a
  strict CSP on `/admin` responses.
- **Secrets**: every secret is a `SecretStr`; boot validation enforces key sizes and
  refuses unsafe production config. CI asserts image history is secret-free.
- **Injection**: no f-string SQL anywhere; the only `text()` SQL is the promo atomic
  claim/release with `:named` params. Media keys are regex-allow-listed and uploads
  verified by magic bytes.
- **Environment interlock**: fakes raise if constructed in production, dev routes and
  docs are development-only, and tests assert all of it.

## Findings and their resolutions

### SEC-1 (High) — FIXED — Ban now ends access immediately

Original defect: `set_user_banned` flipped `users.status` and nothing else; a banned
user kept their access token and could refresh forever.

The fix closes every path:

- `AdminService.set_user_banned` (`app/services/admin_service.py`) revokes **all** of
  the user's live refresh tokens (`AuthRepository.revoke_all_for_user`) and sets an
  `auth:banned:<user_id>` Redis flag with a TTL outliving the access-token lifetime;
  unban deletes the flag.
- `require_auth` (`app/core/deps.py`) checks the ban flag alongside the jti denylist in
  a **single MGET** — no extra Redis round trip per request.
- `AuthService.refresh` and `verify_otp` both reject `UserStatus.BANNED` accounts, so
  neither rotation nor a fresh OTP login can mint tokens for a banned user.

Verified by `test_ban_revokes_live_access_and_refresh`, which bans a live user and
asserts the old access token 401s, the refresh token 401s, and OTP re-login 401s.
Residual note: WebSockets opened before a ban are closed on the next inbound frame
(LOG-2 fix); a fully idle socket persists until it next speaks — acceptable, since it
can neither read new data (the sender path is dead) nor write.

### SEC-2 (Medium) — FIXED — `PAYLINK_ALLOWED_IPS` is enforced

`_enforce_ip_allowlist` (`app/routers/webhooks.py`) now rejects webhook calls from
addresses outside the configured CSV with 403 **before** any body read or signature
work; an empty/unset value (development/test) disables the check, and production boot
still requires it to be set. Verified by two tests (disallowed IP → 403; allowed IP
proceeds to the HMAC gate → 401 on a bad signature). Deployment note: the check reads
`request.client.host`, so terminate TLS/proxying in a way that preserves the source
address (or run the API with a proxy-headers-aware server) — documented in
`docs/endpoints/payments.md`.

### SEC-3 (Medium) — FIXED — OTP HMAC key can never be a constant

The key chain in `OtpService.__init__` is now `OTP_HMAC_KEY` (new optional env var) →
`JWT_SECRET` → `IDENTITY_FINGERPRINT_PEPPER`. Every terminus is a boot-validated
≥32-byte secret present in every algorithm mode; the `"otp"` literal is gone. Verified
by `test_otp_hmac_key_prefers_dedicated_env_var`.

### SEC-4 (Medium) — FIXED — WebSocket chat is guarded

`_pump_socket_to_chat` (`app/routers/chat.py`) now drops frames over
`WS_MAX_FRAME_BYTES` (default 4 KiB) before any decrypt/persist work, throttles each
sender through the shared Redis `RateLimiter` keyed `ws:<user_id>`
(`WS_RATE_LIMIT_MAX_MESSAGES`/`WS_RATE_LIMIT_WINDOW_SECONDS`, default 30/60 s), and
closes the socket (4401) if the sender was banned mid-connection. All three knobs are
env vars. Verified by `test_websocket_send_persists_notifies_and_guards`.

### SEC-5 (Low) — FIXED — Workers use the Lua compare-and-delete lock

All four scheduled jobs (`reconciliation`, `receipts`, `auto_approve`, `expiry`) — and
the new refresh-token purge — now take their lock via `async with redis_lock(...)`
from `app/core/locks.py`, treating `LockNotAcquiredError` as the "already running
elsewhere" skip. The hand-rolled `SET NX EX` + unconditional `DEL` pattern is gone; a
sweep that outlives its TTL can no longer free a peer's lock.

### SEC-6 (Low) — FIXED — No throttle counter can be stranded

The rate limiter's window now advances in **one atomic Lua eval**
(`app/core/ratelimit.py`): INCR, TTL set (with `TTL < 0` self-repair), and ceiling
check in a single round trip. The OTP request/attempt counters use the same
`count == 1 or TTL < 0` repair. A crash between commands can no longer leave a key
without an expiry.

### SEC-7 (Low) — FIXED — Chunked bodies are rejected

`_guard_body_size` (`app/main.py`) rejects a body with no `Content-Length` that
declares `Transfer-Encoding: chunked` with **411 `LENGTH_REQUIRED`** — the JSON API
never needs chunked uploads (media bytes go straight to S3). Oversized declared bodies
still get 413. Verified by `test_chunked_body_without_length_is_rejected`. A reverse
proxy body cap remains recommended as the outer layer.

### SEC-8 (Info) — Accepted — API rate limiter fails open

Re-confirmed this pass: `RateLimiter.check` allows the request on a Redis error
(availability over strict limiting), while the **OTP limiter fails closed** — an
outage on the auth path blocks login, the stricter and correct default. Revisit if the
platform faces credential-stuffing pressure.

### SEC-9 (Info) — Accepted — WS token in the query string

Browsers cannot set headers on WS upgrades; the 30-minute token in `?token=` can land
in proxy logs on the path. If log exposure becomes a concern, mint short-lived
single-use WS tickets over REST. Unchanged, deliberately.

## OWASP API Top-10 quick map (post-fix)

| Risk | Status |
| --- | --- |
| API1 broken object-level auth | Ownership enforced in queries — clean sweep |
| API2 broken authentication | Clean: ban lifecycle closed (SEC-1), OTP key chain safe (SEC-3) |
| API3 property-level auth | Pydantic response schemas; encrypted fields never serialized |
| API4 resource consumption | HTTP + WS throttles, body caps (413/411), pagination caps |
| API5 function-level auth | `require_role` dependencies; admin APIs require ADMIN JWT |
| API6 sensitive business flows | Promo claims atomic; payment idempotency multi-layer |
| API7 SSRF | No user-supplied URLs are fetched server-side |
| API8 misconfiguration | Boot interlock; no dead config remains (SEC-2 enforced) |
| API9 inventory | OpenAPI exported by CI; dev routes gated |
| API10 unsafe 3rd-party consumption | IP gate + HMAC over raw body + amount re-verification |
