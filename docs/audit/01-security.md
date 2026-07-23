# Security audit (current state, 2026-07-21)

## What holds up well

- **JWT handling** (`app/core/jwt.py`): algorithm pinned from config, never read from
  the token header (defeats `alg:none` and RS256→HS256 confusion); `iss`/`aud`/`exp`
  and required claims enforced; a `purpose` claim keeps registration and access tokens
  un-swappable.
- **Session lifecycle** (`app/services/auth_service.py`, `app/core/deps.py`): refresh
  tokens are opaque, stored only as SHA-256, rotated on use, family-revoked on reuse
  (committed before the 401 so the marker survives rollback). A ban now revokes every
  live refresh token, sets an `auth:banned:<user_id>` Redis flag checked in one MGET
  alongside the jti denylist, and both refresh and OTP re-login reject BANNED accounts.
- **Field encryption** (`app/core/crypto.py`): AES-256-GCM, fresh 12-byte CSPRNG IV per
  write, versioned keys, AAD binding each ciphertext to `table:column:entity_id`.
- **OTP** (`app/services/otp_service.py`): CSPRNG codes stored only as HMAC under a key
  that resolves `OTP_HMAC_KEY → JWT_SECRET → IDENTITY_FINGERPRINT_PEPPER` — a validated
  ≥32-byte secret in every mode, never a constant. Request/verify windows self-repair a
  TTL-less counter.
- **Webhook** (`app/routers/webhooks.py`, `paylink/real.py`): source-IP allowlist gate,
  then HMAC-SHA256 over the **raw** body with `compare_digest`, then a Redis lock, then
  the intent status/amount checks, then ledger idempotency keys.
- **Admin surface**: server-side sessions in httponly cookies (`secure` in prod,
  `samesite=strict`, `path=/admin`), stateless HMAC CSRF verified in constant time,
  step-up re-auth, strict `/admin` CSP. The one money-moving admin action (dispute
  resolution) is ADMIN-gated and bounds-checks the split amount (422, never a 500).
- **Injection / traversal**: zero f-string SQL; only `:named`-param `text()` (promo
  atomic claim/release, readiness `SELECT 1`). Media keys are regex-allow-listed and
  uploads verified by magic bytes.
- **Interlock**: fakes raise in production, dev routes and docs are development-only, the
  boot validator refuses unsafe production config, and CI asserts a secret-free image.

## Open findings

### NF-1 (Medium) — No proxy/forwarded-header handling

`request.client.host` is the peer address. It drives three security-relevant decisions:

- the Paylink webhook source-IP allowlist (`app/routers/webhooks.py:_enforce_ip_allowlist`),
- the per-IP rate-limit bucket for unauthenticated requests (`app/main.py:_client_identity`),
- admin audit-log IPs (`app/admin/deps.py:client_ip`).

Behind the documented deploy topology (Gunicorn + Uvicorn workers on ECS/K8s, i.e. an
ALB/ingress in front), that peer is the **proxy**, not the origin. Consequences:

- `PAYLINK_ALLOWED_IPS` set to Paylink's published ranges rejects *every* webhook (they
  arrive from the LB); set to the LB's egress IP it is not an origin filter at all.
- All unauthenticated traffic shares one `ip:<lb>` throttle bucket — a single client can
  exhaust everyone's budget, or the platform under-throttles a real attacker.
- Audit rows record the LB IP, weakening forensics.

This under-cuts the webhook IP-allowlist control that a prior pass added. The correct fix
is **not** application-level `X-Forwarded-For` parsing (trusting a client-set header is a
spoofing footgun); it is to let the ASGI server strip the header against a trusted proxy
list: run Uvicorn/Gunicorn with `--forwarded-allow-ips=<proxy CIDRs>` (never `*`).
Documented in the README ("Running behind a proxy or load balancer"). Left as an open
finding because it is a deploy-config requirement the code cannot enforce for itself, and
a misconfiguration is silent.

### NF-4 (Low) — Live-token ban revocation leans on a best-effort Redis write

`set_user_banned` (`app/services/admin_service.py`) durably revokes refresh tokens and
flips `users.status`, then writes the `auth:banned` Redis flag. If Redis is unavailable
at that moment the flag write fails inside the request; the DB changes still commit, so
the user cannot refresh or re-login, but access tokens already in the wild live until
their ≤30-minute TTL instead of dying at once. The degradation is bounded and safe, but
the "immediate" in SEC-1's guarantee is really "immediate when Redis is up, ≤30 min
otherwise." Note, too, that `require_auth` reads the ban/denylist flags with a bare MGET:
a Redis outage makes that call raise and every authenticated request 500s (fail-closed).
That is the pre-existing denylist behaviour, not new — but it means Redis is a hard
dependency of the authenticated request path, worth stating explicitly.

## Accepted trade-offs (re-confirmed)

- **AT-1** — the API rate limiter fails open on a Redis error (availability over strict
  limiting); the OTP limiter fails closed (an auth path should). Revisit under
  credential-stuffing pressure.
- **AT-2** — the WS access token is in `?token=` (browsers can't set WS upgrade headers);
  it can land in proxy logs. Swap for a short-lived single-use WS ticket if that matters.

## OWASP API Top-10 map

| Risk | Status |
| --- | --- |
| API1 object-level auth | Ownership enforced in queries — clean |
| API2 authentication | Sound; ban lifecycle closed; OTP key chain safe |
| API3 property-level auth | Response schemas; encrypted fields never serialized |
| API4 resource consumption | HTTP+WS throttles, body caps (411/413), pagination caps; NF-1 blunts per-IP throttling behind a proxy |
| API5 function-level auth | `require_role` dependencies; admin APIs require ADMIN JWT |
| API6 sensitive flows | Promo claims atomic; payment idempotency multi-layer; dispute split bounds-checked |
| API7 SSRF | No user-supplied URL is fetched server-side |
| API8 misconfiguration | Boot interlock enforced; NF-1 is the one config-dependent gap |
| API9 inventory | OpenAPI exported by CI; dev routes gated |
| API10 unsafe 3rd-party | IP gate + raw-body HMAC + amount re-verification |
