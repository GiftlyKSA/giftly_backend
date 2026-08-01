# Admin dashboard

The admin dashboard is a **server-rendered** surface at `/admin` (not part of the JSON
API and not in `openapi.json`). It is gated by `ADMIN_DASHBOARD_ENABLED` and calls the
same services as the rest of the backend — it never queries the database directly.

## Authentication

- `GET  /admin/login` — username/password entry form.
- `POST /admin/login` — verifies `ADMIN_USERNAME` and `ADMIN_PASSWORD` using
  constant-time comparisons. Attempts are throttled by both username and source IP.
  The development Compose defaults are `admin` / `admin`; production refuses those
  defaults and requires a password of at least 12 characters.
- A successful login creates or reuses one reserved internal `ADMIN` user solely for
  database foreign keys and audit attribution. Credentials remain environment-only.
- The `admin_session` cookie is `HttpOnly`, `Secure` in production, `SameSite=Strict`,
  and scoped to `Path=/admin`. The DB stores only the SHA-256 of the cookie value.
- `POST /admin/logout` — revokes the session and clears the cookie.

Sessions slide up to an absolute 12-hour cap. Unauthenticated access to any page
redirects (303) to `/admin/login`.

## CSRF and step-up

Because the dashboard uses cookies, **every state-changing form carries a CSRF token**
(a per-session HMAC), verified with `compare_digest`; a bad token returns 403.
`SameSite=Strict` is a second layer.

**Step-up re-authentication** (password confirmation, valid ~5 minutes) is required before:
revealing an identity document or IBAN, changing eligible order delivery details,
verifying a courier, and banning/unbanning a user.

- `POST /admin/step-up/request` — shows the password confirmation form.
- `POST /admin/step-up` — verifies the password and opens the step-up window.

## Pages

| Route | Purpose |
| --- | --- |
| `GET /admin` | overview: order counts, open disputes, pending withdrawals, system balances |
| `GET /admin/tables` | catalog of every application-owned table and its access mode |
| `GET /admin/tables/{table}?page=N` | bounded (50-row), redacted browser for any application table |
| `GET`/`POST /admin/users/new`, `/admin/users` | create a customer or courier user |
| `GET /admin/couriers`, `/admin/couriers/{id}` | list pending and detail; contact fields, city, and bio are visible to signed-in dashboard admins |
| `GET`/`POST /admin/couriers/new`, `/admin/couriers` | create a profile for an existing courier user; encrypt a required national ID or passport |
| `POST /admin/couriers/{id}/delete` | remove a courier profile; retains the user and historical records |
| `POST /admin/couriers/{id}/verify` | approve/reject (step-up + CSRF + audit) |
| `POST /admin/couriers/{id}/reveal-identity` | decrypt identity once (step-up + audit, `no-store`) |
| `GET`/`POST /admin/orders/new`, `/admin/orders` | create a NEW order for an active customer |
| `GET /admin/orders`, `/admin/orders/{id}` | delivery detail; only `NEW`/`ASSIGNED` orders may have city, date, description, or address note changed after step-up |
| `POST /admin/orders/{id}/edit` | controlled non-financial delivery-detail update (step-up + CSRF + audit) |
| `POST /admin/orders/{id}/delete` | permanently delete an unassigned `NEW` order only |
| `GET /admin/invoices`, `/admin/invoices/{id}` | read-only (admins never author invoices) |
| `GET /admin/promos`, `/admin/promos/{id}` | read-only list and detail |
| `GET /admin/promos/{id}/redemptions` | who used it, when, how much |
| `GET /admin/disputes`, `/admin/disputes/{id}` | read-only |
| `GET /admin/withdrawals` | read-only; IBANs masked |
| `GET /admin/wallets`, `/admin/wallets/{id}` | system + user balances |
| `GET /admin/topups` | wallet top-up intents |
| `GET /admin/users/{id}` | detail; contact fields are visible to signed-in dashboard admins |
| `POST /admin/users/{id}/edit` | edit phone, full name, or email (CSRF + audit) |
| `POST /admin/users/{id}/delete` | soft-delete a user and revoke access; retains financial/audit history |
| `POST /admin/users/{id}/ban` · `/unban` | moderate (step-up + CSRF + audit) |
| `GET /admin/audit-logs` | read-only audit trail |

The table catalog is metadata-backed and therefore includes all application tables without
exposing Postgres/PostGIS system tables. It is view-only except for the record links to
the controlled `users`, `courier_profiles`, and `orders` workflows above. Signed-in
dashboard admins can create, edit, and delete these records through their dedicated
forms. Signed-in dashboard admins can see contact fields in the `users` table and linked
user/courier details; tokens, encrypted fields, addresses, and identity fingerprints
remain redacted in generic table views. Pagination is capped at 50 rows per page.

## Money boundary

No dashboard page moves money outside the ledger service. Dispute resolution and
withdrawal processing move escrow funds and are therefore performed through the
double-entry ledger service (Phase 4), not the dashboard; those pages are read-only
here. The dashboard cannot edit payment, invoice, promo, wallet, transaction, or
withdrawal records. User deletion is a soft delete, and physical order deletion is
limited to unassigned `NEW` orders so it cannot erase billing, payment, or fulfillment
history. Every permitted dashboard mutation writes an `audit_logs` row (actor, action,
entity, ip).

## Withdrawal JSON actions

These ADMIN-JWT endpoints serialize each transition with a database row lock and post
paid withdrawals through the double-entry ledger:

| Route | Transition |
| --- | --- |
| `POST /api/admin/withdrawals/{id}/approve` | `REQUESTED → APPROVED`; retains the hold |
| `POST /api/admin/withdrawals/{id}/reject` | `REQUESTED|APPROVED → REJECTED`; releases the hold; body `{ "reason": "..." }` |
| `POST /api/admin/withdrawals/{id}/paid` | `APPROVED → PAID`; courier wallet → `SYSTEM_GATEWAY` |

Repeating a completed action is idempotent. Invalid transitions return
`409 INVALID_STATE_TRANSITION`; unknown IDs return `404 NOT_FOUND`.

## Hardening

Jinja autoescape is on everywhere (never `|safe` on user text). A restrictive CSP is
sent on every `/admin` response: `default-src 'self'; script-src 'self'; object-src
'none'; frame-ancestors 'none'; base-uri 'none'` — no inline scripts, no external
sources. Static assets are served from `/admin/static` with no CDN.
