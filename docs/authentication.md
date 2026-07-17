# Authentication

Phone OTP → JWT. No passwords.

## Flow

1. `POST /api/auth/send-otp {phone}` → `202 {expires_in}`. The response shape and timing
   are identical whether or not the phone is registered (no enumeration). Rate limited
   per phone and per IP.
2. `POST /api/auth/verify-otp {phone, otp}` → `200 {access_token, refresh_token,
   is_new_user, role}`. In development the FakeSmsClient returns the OTP so you can test
   without a real SMS provider.
3. New users then `POST /api/auth/register {full_name, role, ...}` to create the account
   (and, for couriers, an encrypted identity + a `PENDING_VERIFICATION` profile).

## Tokens

- **Access token**: JWT, 30-minute TTL. Send as `Authorization: Bearer <token>`.
- **Refresh token**: 30-day TTL, opaque, stored server-side as a SHA-256 hash with a
  family id. It **rotates on every use**; presenting an old token twice revokes the whole
  family and forces re-auth (reuse detection).
- `POST /api/auth/refresh {refresh_token}` → `200 {access_token, refresh_token}`.
- `POST /api/auth/logout` → `204` (denylists the access token's `jti`).

The access token carries only `sub, role, jti, iat, exp, iss, aud`. Profile data
(name, email, rating) is fetched from `GET /api/users/me`, never read from the token —
those values go stale the instant the user edits them.

## When to refresh

Refresh proactively shortly before the 30-minute access token expires, or reactively on
a `401 UNAUTHORIZED`. If refresh itself returns 401, the family was revoked — send the
user back to `send-otp`.

## WebSocket auth

Chat sockets authenticate **before** the connection is accepted. Obtain a single-use
ticket over authenticated HTTP (`POST /api/conversations/{id}/ws-ticket`) and present it
when opening `WS /api/conversations/{id}/ws`. Never put a token in the WS query string.

## Admin

The admin dashboard is a separate surface (`/admin`) with server-side sessions and
CSRF-protected cookies — it does not use the bearer JWT. See `endpoints/admin.md`.
