# Admin dashboard

The admin dashboard is a **server-rendered** surface at `/admin` (not part of the JSON
API and not in `openapi.json`). It is gated by `ADMIN_DASHBOARD_ENABLED` and calls the
same services as the rest of the backend — it never queries the database directly.

## Authentication

- `GET  /admin/login` — phone entry form.
- `POST /admin/login` — sends an OTP (identical response whether or not the phone is an
  admin; no enumeration). In development the OTP is shown on the page.
- `POST /admin/login/verify` — verifies the OTP; only a `role=ADMIN`, `status=ACTIVE`
  user completes. Sets an `admin_session` cookie: `HttpOnly`, `Secure` (production),
  `SameSite=Strict`, `Path=/admin`. The DB stores only the SHA-256 of the cookie value.
- `POST /admin/logout` — revokes the session and clears the cookie.

Sessions slide up to an absolute 12-hour cap. Unauthenticated access to any page
redirects (303) to `/admin/login`.

## CSRF and step-up

Because the dashboard uses cookies, **every state-changing form carries a CSRF token**
(a per-session HMAC), verified with `compare_digest`; a bad token returns 403.
`SameSite=Strict` is a second layer.

**Step-up re-authentication** (a fresh OTP, valid ~5 minutes) is required before:
revealing an identity document or IBAN, creating/activating/deactivating a promo,
verifying a courier, and banning/unbanning a user.
- `POST /admin/step-up/request` — sends a fresh OTP to the admin's own phone.
- `POST /admin/step-up` — verifies it and opens the step-up window.

## Pages

| Route | Purpose |
| --- | --- |
| `GET /admin` | overview: order counts, open disputes, pending withdrawals, system balances |
| `GET /admin/couriers`, `/admin/couriers/{id}` | list pending, detail (identity masked) |
| `POST /admin/couriers/{id}/verify` | approve/reject (step-up + CSRF + audit) |
| `POST /admin/couriers/{id}/reveal-identity` | decrypt identity once (step-up + audit, `no-store`) |
| `GET /admin/orders`, `/admin/orders/{id}` | read-only |
| `GET /admin/invoices`, `/admin/invoices/{id}` | read-only (admins never author invoices) |
| `GET /admin/promos`, `/admin/promos/new`, `/admin/promos/{id}` | list / create / detail |
| `POST /admin/promos` | create (step-up + CSRF + audit; code normalized upper) |
| `POST /admin/promos/{id}/activate` · `/deactivate` | toggle (step-up + CSRF + audit) |
| `GET /admin/promos/{id}/redemptions` | who used it, when, how much |
| `GET /admin/disputes`, `/admin/disputes/{id}` | read-only |
| `GET /admin/withdrawals` | read-only; IBANs masked |
| `GET /admin/wallets`, `/admin/wallets/{id}` | system + user balances |
| `GET /admin/topups` | wallet top-up intents |
| `GET /admin/users/{id}` | detail |
| `POST /admin/users/{id}/ban` · `/unban` | moderate (step-up + CSRF + audit) |
| `GET /admin/audit-logs` | read-only audit trail |

## Money boundary

No dashboard page moves money outside the ledger service. Dispute resolution and
withdrawal processing move escrow funds and are therefore performed through the
double-entry ledger service (Phase 4), not the dashboard; those pages are read-only
here. Every mutating action writes an `audit_logs` row (actor, action, entity, ip).

## Hardening

Jinja autoescape is on everywhere (never `|safe` on user text). A restrictive CSP is
sent on every `/admin` response: `default-src 'self'; script-src 'self'; object-src
'none'; frame-ancestors 'none'; base-uri 'none'` — no inline scripts, no external
sources. Static assets are served from `/admin/static` with no CDN.
