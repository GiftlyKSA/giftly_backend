# SAFE-GIFT API — integration reference for the React Native app

This is the single document a UI agent needs to wire the SAFE-GIFT mobile app to the
backend. It covers every endpoint the app uses: what it does, whether it needs auth,
the request body, the response, and example input/output with types. It is written for
someone who **cannot read the backend source**.

For the machine-authoritative request/response shapes, `docs/openapi.json` is exported
by CI and always current. This file is the human/agent-facing companion.

---

## 1. Base URL & environments

| Environment | Base URL (example) | Notes |
| --- | --- | --- |
| Local dev | `http://localhost:8000` | OpenAPI UI at `/docs`; dev-only helper routes exist |
| Production | `https://<your-host>` | HTTPS only; `/docs` disabled |

- **Every JSON path is under `/api`.** There is **no** version prefix (`/api/v1` does not exist).
- All requests/responses are JSON (`Content-Type: application/json`) unless stated.
- Send a fixed-length body: chunked uploads without a `Content-Length` are rejected (411).
- Max JSON body is ~1 MiB. Media bytes never go through the API (see §11).

---

## 2. Global conventions (read this first)

### Data types on the wire

| Concept | Wire format | Example | UI handling |
| --- | --- | --- | --- |
| Money | **decimal string**, never a number | `"655.50"` | Parse with a decimal lib, never a JS float |
| Currency | ISO code, SAR by default | `"SAR"` | — |
| Timestamp | ISO-8601 **UTC** string | `"2026-09-12T14:30:00Z"` | Parse as UTC, render local |
| Date | `YYYY-MM-DD` string | `"2026-09-20"` | — |
| ID | UUID string | `"3f2b1c9e-...-uuid"` | Opaque; never construct |
| Phone | Saudi E.164 | `"+966501234567"` | Pattern `^\+9665\d{8}$` |
| Field names | `snake_case` | `delivery_city` | — |

> **Money rule:** treat every `_amount`/`balance`/`available` field as a string. Render
> a price breakdown in the exact order the API returns it. Never do `parseFloat`.

### Pagination (all list endpoints)

Request `?cursor=<opaque>&limit=<n>` — `limit` defaults to 20, max 100. The response
carries `next_cursor` (opaque string) or `null` at the end. Pass it back **verbatim**;
never build a cursor yourself. Lists are newest-first.

### Error envelope (every non-2xx)

```json
{ "error": { "code": "PROMO_USAGE_EXCEEDED", "message": "This promo code is no longer available.", "request_id": "3f2b...-uuid" } }
```

- Branch on `error.code` (a stable machine string) — **do not** parse `message`.
- `message` is safe to show the end user directly.
- `request_id` is also in the `X-Request-ID` response header; include it in bug reports.

### HTTP status codes

| Status | Meaning for the UI |
| --- | --- |
| 200 / 201 / 202 / 204 | Success (204 = no body) |
| 400 | Malformed / out-of-bounds value |
| 401 | Not authenticated, bad/expired token, or account suspended → send to login |
| 403 | Authenticated but not permitted (wrong role/state) → hide the action |
| 404 | Not found, or you have no relationship to the resource |
| 409 | State or lock conflict → refresh and retry |
| 411 | You sent no `Content-Length` (don't stream request bodies) |
| 413 | Request body too large |
| 422 | Semantically invalid (e.g. promo rules, pricing) → show inline error |
| 429 | Rate limited → read `Retry-After` (seconds) and back off |
| 500 | Opaque server error → show a generic message, keep the `request_id` |

### Rate limiting

A global limiter (default 120 requests / 60 s per user) returns **429** with a
`Retry-After` header (seconds). Back off and retry after it. The WebSocket also caps
messages (default 30/min) — excess frames are silently dropped.

### Idempotency

Money-moving calls (`/wallets/topup`, `/invoices/{id}/pay`) are safe to retry because
the server de-duplicates by payment intent/invoice. A withdrawal request additionally
requires `Idempotency-Key: <stable 1–128 character value>`; reuse the same value for
every retry so the funds hold is created exactly once.

---

## 3. Authentication model

SAFE-GIFT uses **phone OTP → JWT**. There are no passwords.

- **Access token**: a 30-minute JWT. Send it on every authenticated request as
  `Authorization: Bearer <access_token>`.
- **Refresh token**: a 30-day rotating opaque token. When a call returns **401**, use
  `/api/auth/refresh` to get a new pair, then retry the original call once.
- **Rotation & reuse detection**: each refresh returns a NEW refresh token; the old one
  dies. If a stolen/duplicated refresh token is reused, the whole token family is
  revoked — the user must log in again.
- **Logout**: `/api/auth/logout` denylists the current access token immediately.
- Store both tokens in the device secure store (Keychain/Keystore), never in plain
  AsyncStorage.

### The login / signup flow (step by step)

```
1. POST /api/auth/send-otp      { phone }                 → 202, OTP sent by SMS
2. POST /api/auth/verify-otp    { phone, otp }            → 200
     • existing user → { is_new_user:false, access_token, refresh_token, role }   → done, go to app
     • new user      → { is_new_user:true,  registration_token }                  → go to step 3
3. POST /api/auth/register      { registration_token, role, ...profile }          → 201 { access_token, refresh_token, role }
```

In **development** only, `send-otp` returns the code in `dev_otp` so you can test
without SMS. In production `dev_otp` is always `null`.

### Recommended client interceptor

- Attach `Authorization: Bearer <access>` to every request except the auth endpoints,
  health, and webhooks.
- On a `401` with code `UNAUTHORIZED`, call `/api/auth/refresh` once; on success retry
  the original request; on failure clear tokens and route to login.
- On a `401` whose message indicates suspension, route to login and clear tokens (the
  account was banned; refresh will also fail).

---

## 4. Endpoint map (mobile app)

Roles: **C** = CUSTOMER, **K** = COURIER, **Any** = either authenticated role,
**Public** = no token. Admin dashboard, gateway webhooks, and dev routes are **not**
part of the mobile app and are omitted here.

| # | Method & path | Auth | Role | What it does |
| --- | --- | --- | --- | --- |
| Auth | POST `/api/auth/send-otp` | Public | — | Send a login OTP by SMS |
|  | POST `/api/auth/verify-otp` | Public | — | Verify OTP → tokens or a registration handoff |
|  | POST `/api/auth/register` | Public* | — | Create an account (*needs `registration_token`) |
|  | POST `/api/auth/refresh` | Public* | — | Rotate tokens (*needs `refresh_token`) |
|  | POST `/api/auth/logout` | Bearer | Any | Revoke the current access token |
| Me | GET `/api/users/me` | Bearer | Any | Get my profile |
|  | PATCH `/api/users/me` | Bearer | Any | Update my profile |
| Wallet | GET `/api/wallets/me` | Bearer | Any | My wallet balances |
|  | POST `/api/wallets/topup` | Bearer | Any | Start a wallet top-up (returns a pay URL) |
|  | POST `/api/wallets/withdrawals` | Bearer | K | Request a courier payout |
|  | GET `/api/wallets/me/transactions` | Bearer | Any | My ledger history (paged) |
| Media | POST `/api/media/upload-urls` | Bearer | Any | Get a pre-signed S3 upload URL |
|  | POST `/api/media/confirm` | Bearer | Any | Confirm an uploaded image is valid |
| Orders | POST `/api/orders` | Bearer | C | Create a gift request |
|  | GET `/api/orders` | Bearer | C | My orders (paged) |
|  | GET `/api/orders/available` | Bearer | K | Nearby open orders (radar, paged) |
|  | GET `/api/orders/{id}` | Bearer | Any | One order (participants only) |
|  | POST `/api/orders/{id}/accept` | Bearer | K | Claim an open order |
|  | POST `/api/orders/{id}/cancel` | Bearer | Any | Cancel (before in-progress) |
|  | POST `/api/orders/{id}/deliver` | Bearer | K | Submit geofenced delivery proof |
|  | POST `/api/orders/{id}/approve` | Bearer | C | Approve delivery → releases payout |
|  | POST `/api/orders/{id}/dispute` | Bearer | Any | Open a dispute |
| Invoices | POST `/api/orders/{id}/invoices` | Bearer | K | Author & issue an invoice |
|  | GET `/api/orders/{id}/invoice` | Bearer | Any | The order's active invoice |
|  | GET `/api/invoices/{id}` | Bearer | Any | One invoice |
|  | POST `/api/invoices/{id}/pay` | Bearer | C | Pay (wallet, gateway, or split) |
|  | POST `/api/invoices/{id}/cancel` | Bearer | K | Cancel a draft/issued invoice |
| Promos | POST `/api/promos/validate` | Bearer | C | Preview a promo on an order's invoice |
| Ratings | POST `/api/orders/{id}/ratings` | Bearer | Any | Rate the other party |
|  | GET `/api/users/{id}/ratings/summary` | Bearer | Any | A user's average rating |
| Chat | GET `/api/conversations` | Bearer | Any | My conversation inbox (paged) |
|  | GET `/api/conversations/{id}/messages` | Bearer | Any | Message history (paged) |
|  | POST `/api/conversations/{id}/messages` | Bearer | Any | Send a message |
|  | POST `/api/conversations/{id}/read` | Bearer | Any | Mark my inbound messages read |
|  | WS `/api/ws/conversations/{id}?token=` | Bearer* | Any | Live message stream (*token in query) |
| Devices | POST `/api/devices` | Bearer | Any | Register a push token |
|  | DELETE `/api/devices` | Bearer | Any | Remove a push token |
| Health | GET `/api/health` | Public | — | Liveness (for your own probes) |

---

## 5. Auth endpoints

### POST `/api/auth/send-otp` — send a login OTP
**Auth:** none. **Returns:** 202.

Request body:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `phone` | string | yes | Saudi E.164, pattern `^\+9665\d{8}$` |

```json
// request
{ "phone": "+966501234567" }
// response 202
{ "expires_in": 180, "dev_otp": null }
```

| Response field | Type | Notes |
| --- | --- | --- |
| `expires_in` | int | Seconds until the OTP expires |
| `dev_otp` | string \| null | The code — **development only**, else `null` |

Errors: `429 RATE_LIMITED` (too many OTP requests for this phone — respect `Retry-After`).

### POST `/api/auth/verify-otp` — verify the code
**Auth:** none. **Returns:** 200.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `phone` | string | yes | Same phone |
| `otp` | string | yes | 6 digits, pattern `^\d{6}$` |

```json
// request
{ "phone": "+966501234567", "otp": "849201" }
// response 200 — existing user
{ "is_new_user": false, "role": "CUSTOMER", "access_token": "eyJ...", "refresh_token": "9f8c...", "registration_token": null }
// response 200 — new user
{ "is_new_user": true, "role": null, "access_token": null, "refresh_token": null, "registration_token": "eyJ..." }
```

| Response field | Type | Notes |
| --- | --- | --- |
| `is_new_user` | bool | If true, go to `/register` with `registration_token` |
| `role` | string \| null | `CUSTOMER` / `COURIER` when tokens are issued |
| `access_token` | string \| null | 30-min JWT (existing user) |
| `refresh_token` | string \| null | 30-day token (existing user) |
| `registration_token` | string \| null | Short-lived, for `/register` (new user) |

Errors: `401 UNAUTHORIZED` (wrong/expired code), `429 RATE_LIMITED`.

### POST `/api/auth/register` — create the account
**Auth:** none, but requires a valid `registration_token`. **Returns:** 201.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `registration_token` | string | yes | From `verify-otp` |
| `role` | string | yes | `CUSTOMER` or `COURIER` |
| `full_name` | string \| null | no | ≤120 chars |
| `email` | string \| null | no | Valid email, ≤255; used only for the paid receipt |
| `dob` | date \| null | no | `YYYY-MM-DD` |
| `city` | string \| null | for couriers | ≤100 chars; **required if `role=COURIER`** |
| `national_id` | string \| null | courier ID | ≤64; a courier needs `national_id` **or** `passport_id` |
| `passport_id` | string \| null | courier ID | ≤64 |

```json
// request — customer
{ "registration_token": "eyJ...", "role": "CUSTOMER", "full_name": "Sara", "email": "sara@example.com" }
// request — courier
{ "registration_token": "eyJ...", "role": "COURIER", "full_name": "Omar", "city": "Jeddah", "national_id": "1122334455" }
// response 201
{ "access_token": "eyJ...", "refresh_token": "9f8c...", "role": "CUSTOMER" }
```

Errors: `401 UNAUTHORIZED` (bad/expired registration token), `409 CONFLICT` (phone or
courier identity already registered), `422 VALIDATION_ERROR` (missing courier fields).

> A courier account starts as `PENDING_VERIFICATION` and cannot accept orders until an
> admin verifies it. Reflect this in the UI (a "verification pending" state).

### POST `/api/auth/refresh` — rotate tokens
**Auth:** none, needs the refresh token. **Returns:** 200.

```json
// request
{ "refresh_token": "9f8c..." }
// response 200
{ "access_token": "eyJ...", "refresh_token": "NEW-2a1f...", "role": "CUSTOMER" }
```

Store the NEW refresh token and discard the old one. Errors: `401 UNAUTHORIZED`
(unknown/expired/reused — on reuse the whole family is revoked; force re-login).

### POST `/api/auth/logout` — revoke the current session
**Auth:** Bearer. **Returns:** 204 (no body). Denylists the current access token; also
drop the refresh token client-side.

---

## 6. Profile (me)

### GET `/api/users/me`
**Auth:** Bearer (any role). **Returns:** 200.

```json
{ "id": "3f2b...-uuid", "phone": "+966501234567", "role": "CUSTOMER", "status": "ACTIVE",
  "full_name": "Sara", "email": "sara@example.com", "rating": "4.80", "rating_count": 12 }
```

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string(uuid) | — |
| `phone` | string | E.164 |
| `role` | string | `CUSTOMER` / `COURIER` |
| `status` | string | `ACTIVE` / `PENDING_VERIFICATION` / `BANNED` |
| `full_name` | string \| null | — |
| `email` | string \| null | — |
| `rating` | string | Average rating, decimal string (e.g. `"4.80"`) |
| `rating_count` | int | Number of ratings received |

### PATCH `/api/users/me`
**Auth:** Bearer. **Returns:** 200 (same shape as GET). Send only the fields you change.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `full_name` | string \| null | no | ≤120 |
| `email` | string \| null | no | Valid email, ≤255 |
| `dob` | date \| null | no | `YYYY-MM-DD` |

```json
// request
{ "full_name": "Sara A." }
```

---

## 7. Wallet & payments

### GET `/api/wallets/me`
**Auth:** Bearer. **Returns:** 200.

```json
{ "balance": "300.00", "held_balance": "50.00", "available": "250.00", "currency": "SAR" }
```

| Field | Type | Notes |
| --- | --- | --- |
| `balance` | string(money) | Total |
| `held_balance` | string(money) | Reserved (e.g. a pending payment hold) |
| `available` | string(money) | `balance − held_balance` — spendable now |
| `currency` | string | `"SAR"` |

### POST `/api/wallets/topup`
**Auth:** Bearer. **Returns:** 201. Starts a gateway top-up; open `payment_url` in a
web view / browser; the wallet is credited when the gateway confirms (asynchronously).

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `amount` | string(money) | yes | Bounded (default 100.00–20000.00) |

```json
// request
{ "amount": "500.00" }
// response 201
{ "payment_intent_id": "7ac2...-uuid", "amount": "500.00", "payment_url": "https://pay.example/pay/abc123" }
```

After the user returns from `payment_url`, re-fetch `/api/wallets/me` (the credit lands
via webhook; poll or refresh on focus). Errors: `422 VALIDATION_ERROR` (amount out of
range), `404 NOT_FOUND` (no wallet).

### POST `/api/wallets/withdrawals` (courier)
**Auth:** Bearer **COURIER**. **Header:** `Idempotency-Key` (required). **Returns:** 201.

```json
// request
{ "amount": "250.00", "iban": "SA0380000000608010167519" }
// response
{ "id": "…", "amount": "250.00", "iban_last4": "7519", "status": "REQUESTED", "rejection_reason": null }
```

The server normalizes and encrypts the Saudi IBAN and moves the amount from `available`
to `held_balance`. A rejection releases it; a paid withdrawal posts a ledger debit.
Never persist or log the plaintext IBAN in the client. Errors include
`409 INSUFFICIENT_FUNDS` and `422 VALIDATION_ERROR`.

### GET `/api/wallets/me/transactions`
**Auth:** Bearer. **Returns:** 200. Paged (`?cursor=&limit=`).

```json
{ "items": [
    { "id": "…", "amount": "-724.50", "type": "PAYMENT", "status": "SETTLED",
      "balance_after": "275.50", "created_at": "2026-09-12T14:30:00Z" }
  ], "next_cursor": "MjAyNi0…|uuid" }
```

| Item field | Type | Notes |
| --- | --- | --- |
| `id` | string(uuid) | — |
| `amount` | string(money) | Signed: negative = debit, positive = credit |
| `type` | string | e.g. `TOPUP`, `PAYMENT`, `ESCROW_RELEASE`, `REFUND` |
| `status` | string | `PENDING` / `SETTLED` / `REVERSED` |
| `balance_after` | string(money) | Running balance |
| `created_at` | string(datetime) | — |

---

## 8. Orders (the core flow)

**Customer** creates an order; **couriers** in that city see it on the radar and one
accepts; the courier issues an invoice; the customer pays into escrow; the courier
delivers with geofenced photo proof; the customer approves (or it auto-approves after
72h); escrow releases the courier payout.

Order `status` values you'll render:
`NEW → ASSIGNED → WAITING_PAYMENT → IN_PROGRESS → DELIVERED → COMPLETED`, plus
`CANCELLED`, `DISPUTED`, and `REFUNDED`. (A freshly created, unassigned order is `NEW`.)

### POST `/api/orders` — create a gift request
**Auth:** Bearer **CUSTOMER**. **Returns:** 201 (`OrderDetail`).

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `description` | string \| null | no | ≤2000 chars |
| `delivery_city` | string | yes | ≤100; matches couriers by city |
| `latitude` | number | yes | −90..90 |
| `longitude` | number | yes | −180..180 |
| `delivery_date` | date | yes | `YYYY-MM-DD`, ≤6 months out |
| `request_media_keys` | string[] | no | 0–3 **confirmed** photo keys (see §11) |

```json
// request
{ "description": "A birthday cake, chocolate", "delivery_city": "Jeddah",
  "latitude": 21.5433, "longitude": 39.1728, "delivery_date": "2026-09-20",
  "request_media_keys": ["orders/pending/2b1f...-uuid.jpg"] }
```

`OrderDetail` response (also returned by GET/accept/cancel/deliver/approve):

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string(uuid) | — |
| `status` | string | See lifecycle above |
| `customer_id` | string(uuid) | — |
| `courier_id` | string \| null | Set once accepted |
| `delivery_city` | string | — |
| `delivery_date` | string(date) | — |
| `description` | string \| null | — |
| `latitude` | number \| null | Exact coords shown to the courier **only once assigned** |
| `longitude` | number \| null | — |
| `total_amount` | string(money) | `"0.00"` until an invoice is paid |
| `assigned_at` | string \| null | — |
| `created_at` | string(datetime) | — |

```json
// response 201
{ "id": "9c2a...-uuid", "status": "NEW", "customer_id": "3f2b...", "courier_id": null,
  "delivery_city": "Jeddah", "delivery_date": "2026-09-20", "description": "A birthday cake, chocolate",
  "latitude": null, "longitude": null, "total_amount": "0.00", "assigned_at": null,
  "created_at": "2026-09-12T14:30:00Z" }
```

### GET `/api/orders` — my orders (customer)
**Auth:** Bearer **CUSTOMER**. Paged. Optional `?status=<STATUS>`. Returns
`OrderListResponse`:

```json
{ "items": [ { "id": "…", "status": "NEW", "delivery_city": "Jeddah",
    "delivery_date": "2026-09-20", "description": "…", "created_at": "…" } ],
  "next_cursor": null }
```

`OrderSummary` item fields: `id`, `status`, `delivery_city`, `delivery_date`,
`description` (nullable), `created_at`. (No exact coordinates in list views.)

### GET `/api/orders/available` — the courier radar
**Auth:** Bearer **COURIER**. Paged. Unassigned (`NEW`) orders in the courier's city.
Same `OrderListResponse` shape. A courier that is not yet verified gets `403 FORBIDDEN`.

### GET `/api/orders/{id}`
**Auth:** Bearer, **participants only** (the customer or the assigned courier). Returns
`OrderDetail`. `404` if you are not a participant.

### POST `/api/orders/{id}/accept` — claim (courier)
**Auth:** Bearer **COURIER**. **Returns:** 200 `OrderDetail` (now `ASSIGNED`, with the
exact coordinates populated). No body. Errors: `409 ORDER_ALREADY_ASSIGNED` (another
courier won — remove it from the radar), `403 FORBIDDEN` (unverified courier).

### POST `/api/orders/{id}/cancel`
**Auth:** Bearer, participant. **Returns:** 200 `OrderDetail`. Allowed before the order
is in progress.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `reason` | string \| null | no | ≤255 |

Errors: `409 INVALID_STATE_TRANSITION` (too late to cancel).

### POST `/api/orders/{id}/deliver` — submit proof (courier)
**Auth:** Bearer **COURIER**. **Returns:** 200 `OrderDetail` (now `DELIVERED`). The
courier must be physically within the delivery geofence (≈200 m).

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `latitude` | number | yes | Courier's current lat, −90..90 |
| `longitude` | number | yes | Courier's current lng, −180..180 |
| `proof_media_keys` | string[] | yes | 1–5 **confirmed** delivery-proof photo keys |
| `note` | string \| null | no | ≤500 |

```json
{ "latitude": 21.5431, "longitude": 39.1725,
  "proof_media_keys": ["orders/proof/7c1a...-uuid.jpg"], "note": "Left with reception" }
```

Errors: `403 OUTSIDE_DELIVERY_GEOFENCE` (too far — show distance guidance, never the
target coords), `409 INVALID_STATE_TRANSITION`.

### POST `/api/orders/{id}/approve` — approve delivery (customer)
**Auth:** Bearer **CUSTOMER**. **Returns:** 200 `OrderDetail` (now `COMPLETED`).
Releases the courier payout from escrow. No body. (If the customer never approves, the
order auto-approves ~72h after delivery.)

### POST `/api/orders/{id}/dispute`
**Auth:** Bearer, participant. **Returns:** 201 `DisputeResponse`.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `reason` | string | yes | 3–1000 chars |

```json
// request
{ "reason": "The cake was the wrong flavour." }
// response 201 (DisputeResponse)
{ "id": "d1e2...-uuid", "order_id": "9c2a...-uuid", "status": "OPEN",
  "reason": "The cake was the wrong flavour.", "resolution_note": null }
```

Disputes are resolved by an admin (customer refund / courier payout / split); the app
just shows the dispute status.

---

## 9. Invoices & promos

### POST `/api/orders/{id}/invoices` — author & issue (courier)
**Auth:** Bearer **COURIER** (the assigned courier). **Returns:** 201 `InvoiceResponse`.
The courier lists items (net of tax) and an optional craft fee; the server computes fees,
tax, discount, and totals.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `items` | array | yes | 1–20 line items |
| `items[].title` | string | yes | 1–120 chars |
| `items[].description` | string \| null | no | ≤500 |
| `items[].unit_price_amount` | string(money) | yes | Net unit price, e.g. `"400.00"` |
| `items[].quantity` | int | yes | 1–999 |
| `items[].tax_rate` | string(rate) | no | Fraction, default `"0.15"` (0–1) |
| `courier_fee_amount` | string(money) | no | Craft/labour, net, default `"0.00"` |
| `promo_code` | string \| null | no | 1–32 chars |

```json
// request
{ "items": [ { "title": "Chocolate cake", "unit_price_amount": "400.00", "quantity": 1, "tax_rate": "0.15" } ],
  "courier_fee_amount": "100.00", "promo_code": null }
```

`InvoiceResponse` (every amount server-computed, all decimal strings):

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string(uuid) | — |
| `order_id` | string(uuid) | — |
| `status` | string | `DRAFT` / `ISSUED` / `PAID` / `CANCELLED` / `EXPIRED` |
| `currency` | string | `"SAR"` |
| `items_net_amount` | string(money) | Sum of line nets |
| `courier_fee_amount` | string(money) | — |
| `service_fee_amount` | string(money) | Platform fee (computed) |
| `discount_amount` | string(money) | Promo discount |
| `net_after_discount_amount` | string(money) | — |
| `tax_amount` | string(money) | Total VAT |
| `total_amount` | string(money) | **What the customer pays** |
| `promo_code` | string \| null | — |
| `issued_at` | string \| null | — |
| `expires_at` | string \| null | Pay before this or the invoice expires |
| `items[]` | array | Per-line computed breakdown (see below) |

Each `items[]` entry: `position` (int), `title`, `description` (nullable),
`unit_price_amount`, `quantity` (int), `tax_rate`, `line_net_amount`,
`line_discount_amount`, `line_taxable_amount`, `line_tax_amount`, `line_total_amount`
— all money/rate fields are strings. Render them in this order.

### GET `/api/orders/{id}/invoice` — the order's active invoice
**Auth:** Bearer, participant. **Returns:** 200 `InvoiceResponse`, or `404` if none.

### GET `/api/invoices/{id}`
**Auth:** Bearer, participant. **Returns:** 200 `InvoiceResponse`.

### POST `/api/invoices/{id}/pay` — pay (customer)
**Auth:** Bearer **CUSTOMER**. **Returns:** 200 `PayInvoiceResponse`. Pays from wallet,
gateway, or a split automatically (wallet first, remainder to the gateway).

No request body. (Safe to retry — server de-dups by invoice.)

```json
// response — fully covered by wallet (settled now)
{ "invoice_id": "…", "status": "PAID", "amount_from_wallet": "724.50", "amount_from_gateway": "0.00", "payment_url": null }
// response — gateway needed (open payment_url, poll the invoice)
{ "invoice_id": "…", "status": "PENDING", "amount_from_wallet": "250.00", "amount_from_gateway": "474.50", "payment_url": "https://pay.example/pay/xyz" }
```

| Field | Type | Notes |
| --- | --- | --- |
| `invoice_id` | string(uuid) | — |
| `status` | string | `PAID` (done) or `PENDING` (awaiting the gateway) |
| `amount_from_wallet` | string(money) | Portion taken from wallet |
| `amount_from_gateway` | string(money) | Portion due at the gateway |
| `payment_url` | string \| null | Present when `status=PENDING` — open it |

When `PENDING`, open `payment_url`; on return, re-fetch the invoice — it becomes `PAID`
and the order moves to `IN_PROGRESS`. Errors: `409 CONFLICT` (not payable / expired),
`409 INVALID_STATE_TRANSITION`, `409 INSUFFICIENT_FUNDS` (a concurrent debit consumed
the hold).

### POST `/api/invoices/{id}/cancel` — (courier)
**Auth:** Bearer **COURIER**. **Returns:** 200 `InvoiceResponse` (now `CANCELLED`).

### POST `/api/promos/validate` — preview a promo (customer)
**Auth:** Bearer **CUSTOMER**. **Returns:** 200 `PromoPreviewResponse`. Use this to show
the discount before paying; it does not consume the promo.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `code` | string | yes | 1–32 chars |
| `order_id` | string | yes | The order whose active invoice to price |

```json
// request
{ "code": "WELCOME10", "order_id": "9c2a...-uuid" }
// response 200
{ "code": "WELCOME10", "discount_amount": "50.00", "original_total_amount": "724.50", "total_amount": "674.50" }
```

Errors (all 422, branch on `code`): `PROMO_NOT_FOUND`, `PROMO_INACTIVE`,
`PROMO_NOT_STARTED`, `PROMO_EXPIRED`, `PROMO_MIN_ORDER_NOT_MET`, `PROMO_USAGE_EXCEEDED`,
`PROMO_USER_LIMIT_REACHED`.

---

## 10. Ratings

### POST `/api/orders/{id}/ratings`
**Auth:** Bearer, participant. **Returns:** 201 `RatingResponse`. Rate the other party on
a completed order.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `score` | int | yes | 1–5 |
| `comment` | string \| null | no | ≤500 |

```json
// request
{ "score": 5, "comment": "Fast and friendly" }
// response 201
{ "id": "r1…-uuid", "order_id": "9c2a...-uuid", "rated_user_id": "k2…-uuid", "score": 5, "comment": "Fast and friendly" }
```

### GET `/api/users/{id}/ratings/summary`
**Auth:** Bearer. **Returns:** 200.

```json
{ "user_id": "k2…-uuid", "average_score": "4.80", "count": 12 }
```

| Field | Type | Notes |
| --- | --- | --- |
| `user_id` | string(uuid) | — |
| `average_score` | string | Decimal string |
| `count` | int | Ratings received |

---

## 11. Media upload (photos)

Bytes **never** pass through the API. Flow:

```
1. POST /api/media/upload-urls  { purpose, content_type, byte_size }  → { upload_url, storage_key, expires_in }
2. PUT the raw image bytes to upload_url   (S3 directly; set Content-Type to the same MIME)
3. POST /api/media/confirm      { storage_key }                        → { storage_key, confirmed: true }
4. Use storage_key in request_media_keys / proof_media_keys
```

### POST `/api/media/upload-urls`
**Auth:** Bearer. **Returns:** 201.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `purpose` | string | yes | `ORDER_REQUEST` or `DELIVERY_PROOF` |
| `content_type` | string | yes | `image/jpeg` or `image/png` |
| `byte_size` | int | yes | > 0 and within the cap (default 10 MiB) |

```json
// request
{ "purpose": "ORDER_REQUEST", "content_type": "image/jpeg", "byte_size": 348192 }
// response 201
{ "upload_url": "https://s3.example/put/...signed...", "storage_key": "orders/pending/2b1f...-uuid.jpg", "expires_in": 300 }
```

### PUT to `upload_url`
Not an API call — upload directly to S3: `PUT <upload_url>` with the raw bytes and the
**same** `Content-Type` and exact `Content-Length` you declared. Do this within
`expires_in` seconds.

### POST `/api/media/confirm`
**Auth:** Bearer. **Returns:** 200. Validates the object exists and is a real image
(magic-bytes checked).

```json
// request
{ "storage_key": "orders/pending/2b1f...-uuid.jpg" }
// response 200
{ "storage_key": "orders/pending/2b1f...-uuid.jpg", "confirmed": true }
```

Only **confirmed** keys may be passed to `request_media_keys` (order create) or
`proof_media_keys` (deliver). Errors: `400 BAD_REQUEST` (bad key, not found, oversized,
or not a valid image).

---

## 12. Chat

Each accepted order has one conversation between its customer and courier. Message
content is encrypted at rest and returned decrypted to participants only. Use REST to
send/list and the WebSocket for live delivery.

### GET `/api/conversations` — inbox
**Auth:** Bearer, participant. Paged (`?cursor=&limit=`).

```json
{ "items": [ { "conversation_id": "c1…-uuid", "order_id": "9c2a…-uuid",
    "other_user_id": "k2…-uuid", "last_message_preview": "On my way",
    "unread_count": 2, "last_message_timestamp": "2026-09-12T14:31:00Z" } ],
  "next_cursor": null }
```

| Item field | Type | Notes |
| --- | --- | --- |
| `conversation_id` | string(uuid) | — |
| `order_id` | string(uuid) | — |
| `other_user_id` | string(uuid) | The other participant |
| `last_message_preview` | string \| null | Decrypted preview |
| `unread_count` | int | For a badge |
| `last_message_timestamp` | string(datetime) | Sort key |

`next_cursor` is an opaque `<iso8601>|<uuid>` string — pass it back verbatim.

### GET `/api/conversations/{id}/messages`
**Auth:** Bearer, participant. Paged, newest first.

```json
{ "items": [ { "id": "m1…-uuid", "conversation_id": "c1…-uuid", "sender_id": "k2…-uuid",
    "message_type": "TEXT", "content": "On my way", "is_read": false,
    "created_at": "2026-09-12T14:31:00Z" } ], "next_cursor": null }
```

| Message field | Type | Notes |
| --- | --- | --- |
| `id` | string(uuid) | Use as `cursor` for the next page |
| `conversation_id` | string(uuid) | — |
| `sender_id` | string(uuid) | — |
| `message_type` | string | `TEXT` |
| `content` | string | Decrypted text |
| `is_read` | bool | — |
| `created_at` | string(datetime) | — |

### POST `/api/conversations/{id}/messages` — send
**Auth:** Bearer, participant. **Returns:** 201 `MessageResponse` (same shape as an item
above).

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `text` | string | yes | 1–4000 chars |

```json
// request
{ "text": "On my way" }
```

### POST `/api/conversations/{id}/read`
**Auth:** Bearer, participant. **Returns:** 204. Marks the caller's inbound messages read
and clears their unread count.

### WS `/api/ws/conversations/{id}?token=<access_token>`
**Auth:** the access token in the **query string** (browsers/RN can't set headers on the
WS upgrade). Only the two participants may connect.

- **Server → client**: each new message is pushed as a JSON object with the same fields
  as `MessageResponse` (`id`, `conversation_id`, `sender_id`, `message_type`, `content`,
  `is_read`, `created_at`).
- **Client → server**: send a JSON frame `{ "text": "..." }` to post a message (same
  effect as the REST POST, including the recipient push). Non-JSON frames, frames larger
  than ~4 KiB, and messages beyond the per-user rate (default 30/min) are silently
  dropped — so treat the **REST POST as the source of truth** when you must confirm a
  send. A close code `4401` means unauthenticated (or the account was suspended
  mid-session); `4403` means you are not a participant.

Client pattern: open the WS for the live stream, but **send via REST** for guaranteed
delivery and the returned message id; use the WS only to receive.

---

## 13. Push notifications (devices)

Register the device's push token after login (and on token refresh). Notifications are
best-effort and never contain sensitive content (e.g. a chat push says "New message",
never the text).

### POST `/api/devices`
**Auth:** Bearer. **Returns:** 201.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `token` | string | yes | 1–512 chars (FCM/APNs token) |
| `device_os` | string | yes | `IOS` or `ANDROID` (else 422) |

```json
// request
{ "token": "fcm-abc123", "device_os": "IOS" }
// response 201
{ "token": "fcm-abc123", "device_os": "IOS" }
```

A token is unique across users, so registering a handed-down device re-points it to the
new owner.

### DELETE `/api/devices`
**Auth:** Bearer. **Returns:** 204. Body: `{ "token": "fcm-abc123" }`. Removes the token
if it belongs to the caller. Call on logout.

Push events the app receives: **new order nearby** (couriers in the city), **new
message** (the recipient). Bodies carry no restricted content — fetch details via REST.

---

## 14. End-to-end journeys

### Customer
1. `send-otp` → `verify-otp` → (`register` if new) → store tokens.
2. `POST /api/devices` with the push token.
3. (Optional) upload request photos: `media/upload-urls` → PUT → `media/confirm`.
4. `POST /api/orders` → order is `NEW`.
5. Wait for a courier to accept (push / poll `GET /api/orders/{id}` → `ASSIGNED`).
6. Courier issues an invoice → `GET /api/orders/{id}/invoice` (`WAITING_PAYMENT`).
7. (Optional) `POST /api/promos/validate` to preview a code.
8. `POST /api/invoices/{id}/pay` → if `PENDING`, open `payment_url`, then re-fetch.
9. Chat with the courier (`/conversations/...` + WS).
10. On delivery (`DELIVERED`), `POST /api/orders/{id}/approve` → `COMPLETED`.
11. `POST /api/orders/{id}/ratings`.

### Courier
1. `send-otp` → `verify-otp` → `register` (role `COURIER`, city + id). Account is
   `PENDING_VERIFICATION` until an admin verifies — show a waiting state.
2. `POST /api/devices`.
3. `GET /api/orders/available` (radar) → `POST /api/orders/{id}/accept`.
4. `POST /api/orders/{id}/invoices` → invoice `ISSUED`.
5. Wait for payment (`IN_PROGRESS`), chat as needed.
6. At the drop-off: upload proof photos, then `POST /api/orders/{id}/deliver` (must be
   inside the geofence).
7. Payout releases on customer approval / auto-approval; see it in
   `GET /api/wallets/me/transactions`.
8. `POST /api/orders/{id}/ratings`.

---

## 15. Error code catalogue (branch on `error.code`)

| `code` | Status | Meaning | Suggested UI |
| --- | --- | --- | --- |
| `UNAUTHORIZED` | 401 | Missing/invalid/expired token, or suspended | Refresh once, else login |
| `FORBIDDEN` | 403 | Not permitted for this role/state | Hide/disable the action |
| `NOT_FOUND` | 404 | No such resource / not yours | Generic "not found" |
| `INVALID_STATE_TRANSITION` | 409 | Action illegal from the current state | Refresh state, re-derive actions |
| `CONFLICT` | 409 | Concurrent op won, or uniqueness hit | Refresh and retry |
| `ORDER_ALREADY_ASSIGNED` | 409 | Another courier accepted first | Remove from the radar |
| `INSUFFICIENT_FUNDS` | 409 | Available balance can't cover it | Prompt a top-up |
| `VALIDATION_ERROR` | 422 | Semantically invalid input | Inline field error |
| `PROMO_*` | 422 | Promo rule failed (see §9) | Inline promo error |
| `OUTSIDE_DELIVERY_GEOFENCE` | 403 | Courier too far from drop-off | Distance guidance (never show target coords) |
| `PAYLOAD_TOO_LARGE` | 413 | Body over the size cap | Shrink payload |
| `LENGTH_REQUIRED` | 411 | No `Content-Length` sent | Don't stream request bodies |
| `RATE_LIMITED` | 429 | Too many requests | Respect `Retry-After` |
| `INTERNAL_ERROR` | 500 | Unexpected server error | Generic message; keep `request_id` |

---

## 16. Out of scope for the mobile app

These exist but are **not** for the RN client: the admin dashboard (`/admin`, server-
rendered HTML), the gateway webhook (`POST /api/webhooks/streampay`, called by StreamPay,
not the app), and the development-only helpers (`/api/dev/*`). Ignore them.
