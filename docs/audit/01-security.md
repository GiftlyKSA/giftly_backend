# Security audit (2026-08-01)

## Verdict

Authentication, authorization, cryptography, webhook verification, admin controls, and
production interlocks are structurally sound. No high-confidence code vulnerability was
found by review or Bandit. Production must still be blocked on live vendor-contract
verification (OPEN-1) and ownership-checked private-media delivery (OPEN-2).

## Verified controls

- JWT algorithms are configuration-pinned; issuer, audience, expiry, purpose, and
  required claims are enforced. Missing key material fails explicitly even under
  optimized Python.
- Refresh tokens are opaque and hashed, rotate on use, revoke families on replay, and
  are revoked on ban. Login and refresh both consult durable user status.
- AES-256-GCM field encryption uses random IVs, versioned keys, and entity/column AAD.
  Withdrawal IBANs are normalized, encrypted before persistence, and only last four
  digits are returned.
- OTP values are CSPRNG-generated and stored as keyed HMACs with bounded attempts,
  repaired TTLs, and block windows.
- Dashboard credentials remain environment-only and are compared in constant time.
  Attempts are throttled by hashed username and source IP, and Redis failure denies
  authentication. Production rejects development defaults; sessions retain hashed
  cookies, CSRF, audit attribution, and password step-up.
- Ownership is enforced in repository queries; JSON admin money actions require an
  ADMIN JWT; dashboard mutations retain CSRF, strict cookies, and step-up controls.
- The dashboard's metadata-backed table browser is bounded to 50 rows and redacts
  contact, token, encrypted, identity, and address fields. Its generic data views are
  read-only; the three controlled edit workflows require step-up and write audit rows.
- Paylink webhooks check trusted peer IP, then constant-time HMAC over raw bytes, then
  lock/status/amount/idempotency controls. Trusted forwarded headers are accepted only
  from configured proxy IPs/CIDRs.
- Upload keys are server-generated and allow-listed. S3 upload signatures bind MIME and
  exact length; confirmation checks HEAD metadata and magic bytes. CloudFront URLs are
  short-lived and RSA/SHA-256 signed.
- Production boot now requires Paylink, email, SMS, push, S3, and CloudFront settings;
  fake clients independently refuse production construction.
- No f-string SQL, user-supplied fetch URL, committed secret, or production fake path
  was found. `pip-audit --strict` reports no known dependency vulnerability.

## Findings

### OPEN-1 (High) — external security contracts are not proven

The Paylink request/response mapping, sndr payload, SMS endpoint, and Supabase push edge
function are marked `VENDOR CONTRACT`. Local tests validate isolation, HMAC behavior,
timeouts, and HTTP error propagation, but not the providers' actual schemas, replay
headers, authentication requirements, or error bodies. Run provider sandbox contract
tests and pin those schemas before enabling production credentials.

### OPEN-2 (Medium) — signed media reads are not ownership-wired

The storage adapter can sign a private CloudFront URL, but no route calls it. Returning
raw storage keys is not enough for a private bucket, while exposing a generic signer
without an ownership query would create an object-level authorization bug. Add signed
URLs only to order/conversation reads that already prove participant access.

### OPEN-5 (Low) — ban immediacy depends on Redis durability

A ban request is transactional: if the Redis flag write fails, the request raises and
the database changes roll back, so the prior audit's “DB commits while Redis fails”
scenario was incorrect. After a successful ban, however, loss of Redis data can remove
the live-token flag. Refresh/re-login remain blocked by PostgreSQL, but an issued access
token can work until its at-most-30-minute expiry. A durable auth epoch/status check is
the stronger future design.

## Accepted risks

- HTTP throttling fails open for availability. Authentication throttles and WebSocket
  combined guards fail closed.
- WebSocket bearer tokens are in the query string. Redact query strings at every proxy.
- ADMIN JWT withdrawal settlement does not use dashboard step-up; keep ADMIN access
  short-lived and strongly protected until a unified step-up token exists.
