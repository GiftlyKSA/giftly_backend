# Security audit

## What holds up well

Before the findings, the controls that were verified and are correctly built:

- **JWT handling** (`app/core/jwt.py`): the algorithm is pinned from config and never
  read from the token header (defeats `alg:none` and RS256→HS256 confusion); `iss`,
  `aud`, `exp`, and required claims are all enforced; registration tokens carry a
  `purpose` claim and access-token decoding rejects any token that has one
  (`jwt.py:93-94`), so the two token types cannot be swapped.
- **Refresh tokens** (`app/services/auth_service.py:191-219`): opaque, stored only as
  SHA-256, rotated on use, family-revoked on reuse — and the family revoke is committed
  explicitly before the 401 propagates (`auth_service.py:207-209`) so the compromise
  marker survives the request rollback. This is a subtle detail many implementations
  get wrong.
- **Field encryption** (`app/core/crypto.py`): AES-256-GCM with a fresh 12-byte CSPRNG
  IV per write, versioned keys, and an AAD binding every ciphertext to
  `table:column:entity_id` — a blob copied between rows fails to decrypt. Key rotation
  respects the append-only messages table.
- **Webhook verification** (`app/integrations/paylink/real.py:60-63`,
  `app/routers/webhooks.py:28`): HMAC-SHA256 over the **raw** body with
  `hmac.compare_digest`; the body is read once and passed as bytes, never re-serialized.
- **Admin surface** (`app/admin/deps.py`, `app/admin/router.py:88-96`): server-side
  sessions in httponly cookies (`secure` in production, `samesite=strict`,
  `path=/admin`), HMAC-derived stateless CSRF tokens verified in constant time, step-up
  re-auth for sensitive actions, and a strict CSP applied to `/admin` responses.
- **Secrets**: every secret is a `SecretStr`; the boot validator enforces key sizes,
  rejects a pepper equal to any encryption key, and refuses production boot on unsafe
  config (`app/core/config.py:147-225`). The Dockerfile never bakes a secret and CI
  asserts image history is clean.
- **Injection**: no f-string SQL anywhere (swept); the only `text()` SQL is the promo
  atomic claim/release with `:named` bound params
  (`app/repositories/promo_repository.py:93-120`). Media keys are validated against a
  strict regex allow-list with traversal rejection (`app/services/media_service.py:25,87`),
  and uploads are verified by magic bytes, not extension.
- **Environment interlock**: fakes raise if constructed in production
  (`app/integrations/_guard.py`), dev routes are registered only in development, docs
  are disabled outside development, and tests assert all of it.

## Findings

### SEC-1 (High) — Banning a user does not revoke their live access

`AdminService.set_user_banned` (`app/services/admin_service.py:230-244`) sets
`users.status = BANNED` and writes an audit row — and does nothing else. Meanwhile:

- `require_auth` (`app/core/deps.py:58-83`) validates the JWT and checks the jti
  denylist, but never loads the user, so it cannot see the BANNED status.
- `AuthService.refresh` (`app/services/auth_service.py:191-219`) loads the user and
  checks only that the row exists — not its status — before issuing a fresh access +
  refresh pair.

Consequence: a banned user keeps their current access token for up to 30 minutes **and
can refresh indefinitely**, retaining full API access forever. Only a fresh OTP login is
implicitly blocked (and only where a flow checks status, e.g. courier accept at
`app/services/order_service.py:198`).

**Recommendation**: in `set_user_banned`, (a) revoke all of the user's refresh-token
families, and (b) either denylist their outstanding jtis or add a per-user
`auth:banned:<user_id>` Redis flag checked in `require_auth` (one GET, mirroring the
existing denylist check). Add a status check to `refresh` regardless.

### SEC-2 (Medium) — `PAYLINK_ALLOWED_IPS` is required but never enforced

The production interlock refuses to boot without `PAYLINK_ALLOWED_IPS`
(`app/core/config.py:208-216`), but no code reads it afterwards — the only references
are the config declaration and the validator. The webhook route
(`app/routers/webhooks.py`) authenticates by HMAC alone.

The HMAC is the primary control and it is sound, so this is defence-in-depth, not a
hole. But config that promises a control the code doesn't apply is worse than no
config: an operator reading `.env.example` reasonably believes source-IP filtering is
active. Either enforce it in the webhook route (parse the CSV once at boot, compare
`request.client.host`, honouring the deployment's proxy header policy) or delete the
setting and let the reverse proxy own IP filtering explicitly.

### SEC-3 (Medium) — OTP HMAC key falls back to the literal string `"otp"`

`app/services/otp_service.py:28`:

```python
self._hmac_key = settings.JWT_SECRET.get_secret_value() if settings.JWT_SECRET else "otp"
```

Under `JWT_ALGORITHM=RS256`, `JWT_SECRET` is legitimately `None` (the validator only
requires it for HS256, `app/core/config.py:176-184`), so OTP hashes would be HMAC'd
under a known constant. An attacker with a Redis dump could then precompute all 10⁶
digests and recover every in-flight OTP — exactly the "Redis dump must not be a free
login" scenario the HMAC exists to prevent. Today's deployments use HS256 so the branch
is dormant, but it is a landmine armed by a config change.

**Recommendation**: derive the OTP key from a secret that exists in every mode (e.g.
`IDENTITY_FINGERPRINT_PEPPER`, or a dedicated `OTP_HMAC_KEY`), and make the boot
validator reject the RS256 + no-usable-OTP-key combination.

### SEC-4 (Medium) — WebSocket chat is outside every throttle

The Phase 14 rate limiter is HTTP middleware; a WebSocket upgrade passes through it
once, and after that `_pump_socket_to_chat` (`app/routers/chat.py:201-220`) loops on
`receive_text` with no per-connection message-rate cap, no message-size cap beyond
schema-free `str`, and no unread-counter damping. A hostile but authenticated
conversation member can write messages (each an encrypted DB row + Redis publish) as
fast as the socket allows.

**Recommendation**: apply the existing `RateLimiter` inside the receive loop keyed
`ws:<user_id>` (it is already Redis-backed and cheap), and reject frames over a few KB
before decrypt/persist.

### SEC-5 (Low) — Scheduled workers release their Redis lock with a plain `DEL`

The repo's own known-traps list says a plain `DEL` can release someone else's lock, and
`app/core/locks.py` implements the correct Lua compare-and-delete — but all four
scheduled jobs bypass it and hand-roll `SET NX EX` + unconditional `DELETE`:

- `app/workers/reconciliation.py:54,60`
- `app/workers/receipts.py:85,91`
- `app/workers/auto_approve.py:86,92`
- `app/workers/expiry.py:147,153`

If a sweep ever runs longer than its TTL (300–600 s), the lock expires, a second worker
acquires it, and the first worker's `finally: DEL` releases the second worker's lock —
inviting a third concurrent run. The sweeps are individually idempotent (per-row locks
and re-checks), so this degrades to wasted work rather than corruption, but it
contradicts the codebase's own rule and `redis_lock` is sitting right there.

**Recommendation**: replace the hand-rolled pattern in all four workers with
`async with redis_lock(...)`, treating `LockNotAcquiredError` as the existing
"already running elsewhere" skip.

### SEC-6 (Low) — `INCR` then `EXPIRE` can strand a counter without a TTL

Both throttles use the same non-atomic idiom: `app/core/ratelimit.py:55-58` and
`app/services/otp_service.py:55-57` first `INCR`, then set the expiry only when the
count is 1. If the process dies (or Redis errors) between the two commands on the first
hit, the key lives forever with no TTL — permanently rate-limiting that identity or
phone once the ceiling is crossed. Improbable per-request, but with every request
passing through the limiter it will eventually happen to someone, and the symptom
(one user forever 429'd) is miserable to diagnose.

**Recommendation**: make the window atomic — a two-line Lua script
(`INCR` + `EXPIRE NX`), or check `TTL == -1` after `INCR` and repair.

### SEC-7 (Low) — Body-size guard trusts `Content-Length`

`_body_size_guard` (`app/main.py:169-185`) rejects on a declared `Content-Length` over
the cap, but a request using chunked transfer encoding carries no such header and
streams past the guard; FastAPI will still buffer the body when a route awaits it.
JSON endpoints are the only consumers (media bytes go straight to S3), so exposure is
memory pressure, not parsing pathology.

**Recommendation**: keep the guard as the fast path, and enforce a hard cap at the
reverse proxy (`client_max_body_size` or equivalent) in the deployment docs. Optional:
count bytes in a `receive` wrapper for true streaming enforcement.

### SEC-8 (Info) — Rate limiter fails open by design

`RateLimiter.check` (`app/core/ratelimit.py:62-64`) allows the request on any Redis
error, logging a warning. This is a documented decision (DECISIONS.md, 2026-07-19):
availability over strict limiting. It means a Redis outage suspends throttling exactly
when an attacker might be causing it — accepted, but it should be revisited if the
platform ever faces credential-stuffing pressure, since OTP throttling shares the same
Redis. (The OTP limiter, by contrast, fails closed — an outage there raises and blocks
login, the stricter and correct default for an auth path.)

### SEC-9 (Info) — WS access token in the query string

`app/routers/chat.py` authenticates the WebSocket via `?token=`, so the (30-minute)
access token can be captured in proxy/access logs along the path. Documented trade-off;
browsers cannot set headers on WS upgrades. If log exposure matters later, mint a
short-lived single-use WS ticket over REST and pass that instead.

## OWASP API Top-10 quick map

| Risk | Status |
| --- | --- |
| API1 broken object-level auth | Ownership enforced in queries (`get_for_actor` pattern) — clean sweep |
| API2 broken authentication | Sound except SEC-1 (ban lifecycle) and SEC-3 (dormant weak key) |
| API3 property-level auth | Pydantic response schemas; encrypted fields never serialized |
| API4 resource consumption | Rate limiter + body cap + pagination caps; gaps: SEC-4 (WS), SEC-7 |
| API5 function-level auth | `require_role` dependencies; admin APIs require ADMIN JWT |
| API6 sensitive business flows | Promo claims atomic; payment idempotency 3-layer |
| API7 SSRF | No user-supplied URLs are fetched server-side |
| API8 misconfiguration | Boot interlock; but see SEC-2 (dead config) |
| API9 inventory | OpenAPI exported by CI; dev routes gated |
| API10 unsafe 3rd-party consumption | Webhook HMAC over raw body; amounts re-verified against intents |
