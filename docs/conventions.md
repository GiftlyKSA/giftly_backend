# Conventions

Cross-cutting rules that every endpoint and every screen relies on.

## Base path & methods

- All JSON endpoints are under `/api`. There is **no** version prefix (`/api/v1` is
  forbidden). The admin dashboard is a separate HTML surface under `/admin`.
- `GET` reads, `POST` creates/actions, `PATCH` partial updates, `DELETE` soft-deletes.

## Casing & dates

- JSON field names are `snake_case`.
- All timestamps are ISO-8601 in **UTC** (e.g. `2026-09-12T14:30:00Z`). Dates are
  `YYYY-MM-DD`.

## Money

- **Money is always a decimal STRING**, never a JSON number: `"655.50"`, not `655.5`.
- Never parse money into a JS float; use a decimal library. Render a breakdown in the
  exact order the API returns it.
- Currency is SAR unless a field says otherwise.

## Pagination

- Keyset (cursor) pagination everywhere; `OFFSET` is never used.
- Request `?cursor=<opaque>&limit=<n>`; `limit` defaults to 20, max 100.
- Responses carry `next_cursor` (base64 of the last `(created_at, id)`), or null at the
  end. Pass it back verbatim.

## Idempotency

- Money-moving endpoints **require** an `Idempotency-Key` header (a client-generated
  UUID). Retrying with the same key returns the original result and never double-charges.

## Error envelope

Every error looks like this:

```json
{
  "error": {
    "code": "PROMO_USAGE_EXCEEDED",
    "message": "This promo code is no longer available.",
    "request_id": "3f2b...-uuid"
  }
}
```

- `code` is a stable machine string — branch on it, do not parse `message`.
- `message` is safe to show the end user.
- `request_id` is echoed in the `X-Request-ID` response header; include it in bug
  reports.

## HTTP status codes

| status | meaning |
| --- | --- |
| 200 | OK |
| 201 | created |
| 202 | accepted (async, e.g. OTP queued) |
| 204 | no content |
| 400 | request validation failed |
| 401 | unauthenticated or bad signature |
| 403 | authenticated but not permitted |
| 404 | not found, or the caller has no relationship to the resource |
| 409 | state or lock conflict |
| 413 | request body too large |
| 422 | semantically invalid (promo/pricing) |
| 429 | rate limited (see `Retry-After`) |
| 500 | opaque server error |

## Rate limiting & body size

Every request is throttled by a global fixed-window limiter (default **120 requests /
60 s**). The window is keyed per identity: the authenticated user for a signed request,
otherwise the client IP. Exceeding it returns **429 `RATE_LIMITED`** with a `Retry-After`
header (seconds until the window resets) — surface a countdown and retry after it. The
liveness/readiness probes (`/api/health*`) are never throttled.

Request bodies are capped (default **1 MiB**); an oversized body is rejected up front
with **413 `PAYLOAD_TOO_LARGE`** before any route runs. Media bytes never transit the API
— clients `PUT` straight to S3 with a pre-signed URL — so this cap only bounds JSON.

## Error code catalogue

| code | status | meaning | UI hint |
| --- | --- | --- | --- |
| `UNAUTHORIZED` | 401 | missing/invalid/expired token or bad webhook signature | send to login / refresh |
| `FORBIDDEN` | 403 | related to the resource but not permitted this action/role/state | hide or disable the action |
| `NOT_FOUND` | 404 | no such resource, or no relationship to the caller | generic "not found" |
| `INVALID_STATE_TRANSITION` | 409 | action illegal from the current state | refresh state, re-derive actions |
| `CONFLICT` | 409 | a concurrent op won a lock, or a uniqueness rule was hit | refresh and retry |
| `ORDER_ALREADY_ASSIGNED` | 409 | another courier accepted first | remove from available list |
| `VALIDATION_ERROR` | 422 | semantically invalid input | show inline error |
| `PROMO_NOT_FOUND` | 422 | unknown code | inline "code not recognised" |
| `PROMO_INACTIVE` | 422 | code disabled | inline error |
| `PROMO_NOT_STARTED` | 422 | before `starts_at` | inline error |
| `PROMO_EXPIRED` | 422 | after `ends_at` | inline error |
| `PROMO_MIN_ORDER_NOT_MET` | 422 | order below `min_order_amount` | inline error |
| `PROMO_USAGE_EXCEEDED` | 422 | global cap reached | inline error, drop the code |
| `PROMO_USER_LIMIT_REACHED` | 422 | this user's per-user cap reached | inline error |
| `TOPUP_AMOUNT_OUT_OF_RANGE` | 400 | top-up not in [100, 20000] | inline error |
| `OUTSIDE_DELIVERY_GEOFENCE` | 403 | courier too far from the drop-off | show distance guidance (never the target coords) |
| `PAYLOAD_TOO_LARGE` | 413 | request body over the size cap | shrink the payload |
| `RATE_LIMITED` | 429 | too many attempts | show `Retry-After` countdown |

New codes are added to this table as their endpoints land — it is the single source of
truth for `error.code`.
