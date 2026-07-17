# Auth

OTP → JWT. See `../authentication.md` for the overall model. All bodies are JSON.

## POST /api/auth/send-otp
Request a login OTP. **Auth**: none.
### Body
| field | type | required | constraints |
| phone | string | yes | Saudi E.164 mobile `^\+9665\d{8}$` |
### Success 202 — SendOtpResponse
```json
{ "expires_in": 180, "dev_otp": "849201" }
```
`dev_otp` is present only in development. The response shape and timing are identical
whether or not the phone is registered (no enumeration). Rate limited per phone.
### Errors
| status | code | when |
| 429 | RATE_LIMITED | too many requests for this phone (see `Retry-After`) |

## POST /api/auth/verify-otp
Verify an OTP. **Auth**: none.
### Body
| field | type | required | constraints |
| phone | string | yes | Saudi E.164 mobile |
| otp | string | yes | 6 digits |
### Success 200 — VerifyOtpResponse
Existing user: `{ "is_new_user": false, "role": "CUSTOMER", "access_token": "...", "refresh_token": "..." }`
New user: `{ "is_new_user": true, "registration_token": "..." }`
### Errors
| status | code | when |
| 401 | UNAUTHORIZED | wrong or expired code |

## POST /api/auth/register
Create the account authorised by a registration token. **Auth**: none (uses the token).
### Body
| field | type | required | notes |
| registration_token | string | yes | from verify-otp |
| role | enum | yes | CUSTOMER \| COURIER |
| full_name | string | no | ≤120 |
| email | string | no | used only for the paid receipt |
| dob | date | no | |
| city | string | courier-only | required for couriers |
| national_id / passport_id | string | courier-only | one required; encrypted at rest |
### Success 201 — TokenResponse
```json
{ "access_token": "...", "refresh_token": "...", "role": "CUSTOMER" }
```
A courier is created `PENDING_VERIFICATION` and cannot accept orders or invoice until an
admin verifies them. Their identity is encrypted (AES-256-GCM) and a blind-index
fingerprint blocks duplicate registrations.
### Errors
| status | code | when |
| 401 | UNAUTHORIZED | registration token invalid/expired |
| 409 | CONFLICT | phone or identity already registered |
| 422 | VALIDATION_ERROR | courier missing city/identity, or role=ADMIN |

## POST /api/auth/refresh
Rotate a refresh token. **Auth**: none (uses the token).
### Body: `{ "refresh_token": "..." }` → 200 TokenResponse (a new pair)
Refresh tokens rotate on every use. Presenting an already-used or revoked token is
treated as compromise: the whole token **family** is revoked and re-auth is forced.
### Errors
| status | code | when |
| 401 | UNAUTHORIZED | unknown, expired, or reused token (reuse revokes the family) |

## POST /api/auth/logout
Revoke the current access token. **Auth**: Bearer JWT. → 204. The token's `jti` is
denylisted until it would have expired.
