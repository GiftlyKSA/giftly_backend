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

- `api-4-ui.md` — **the one-file integration reference for the React Native app**:
  every mobile endpoint with auth, request/response field tables (name, type, required),
  example input/output, the OTP→JWT auth flow, and end-to-end journeys. Start here to
  build UI.
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
- `audit/` — the post-build self-audit (security, money integrity, logic, performance,
  guideline compliance) with severity-ranked findings. Not needed for UI building, but
  authoritative on known limitations.

## Status

**Complete.** All 14 phases of the master spec (SECTION 25) are implemented and
documented: every endpoint group under `endpoints/`, the flows, the models, and the
exported `openapi.json` reflect the shipped API. A route change without a doc change
still fails review — these docs stay in lockstep with the contract. Known limitations
are catalogued in `audit/`.
