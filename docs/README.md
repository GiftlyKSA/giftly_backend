# SAFE-GIFT API docs

These docs are written for a **UI-building agent that cannot read the backend source**
and cannot ask questions. Everything needed to build a screen should be here.

## How to read these docs

- **Base URL**: all JSON endpoints live under `/api` (no version prefix).
- **Auth model**: OTP → JWT. A 30-minute access token in the `Authorization: Bearer`
  header; a 30-day rotating refresh token. See `authentication.md`.
- **Conventions**: casing, dates, money-as-strings, pagination, idempotency, and the
  error envelope with every error `code` — see `conventions.md`. Read this first.
- **The machine contract**: `openapi.json` is exported by CI and is authoritative for
  request/response shapes.

## Contents

- `conventions.md` — cross-cutting rules every screen relies on.
- `authentication.md` — the OTP→JWT flow, refresh rotation, and WS tickets.
- `openapi.json` — the exported OpenAPI schema.
- `endpoints/` — one file per resource group (auth, users, couriers, media, orders,
  invoices, promos, payments, wallets, chat, admin). Each endpoint documents auth, role,
  required state, path/body params, a success example, and every error `code`.
- `flows/` — end-to-end journeys: order lifecycle, invoice & pricing, split payment,
  top-up, delivery & geofence, dispute, chat.
- `models/` — `enums.md` (every enum value + label) and `schemas.md` (every
  request/response object, field by field).

## Status

The platform is being built in the phase order of the master spec (SECTION 25). The
foundational layers (config, money, pricing, crypto, the data schema) and their docs
(`conventions.md`, `flows/invoice-and-pricing.md`, `models/enums.md`) are in place.
Endpoint docs land with each endpoint's phase; a route change without a doc change fails
review.
