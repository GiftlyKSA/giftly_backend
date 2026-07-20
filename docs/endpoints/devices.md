# Devices & notifications

## POST /api/devices
Register (or refresh) a push token for the caller's device. **Role**: CUSTOMER or COURIER.
A token is unique across users, so registering a handed-down device re-points it to the
new owner and drops it from the previous one. → 201 `{ token, device_os }`.
### Body
| field | notes |
| token | 1–512 chars |
| device_os | `IOS` or `ANDROID` (else 422) |

## DELETE /api/devices
Remove a push token, but only if it belongs to the caller (ownership in the query). → 204.
### Body
`{ "token": "..." }`

## Notifications (SPEC SECTION 13)
Push notifications are **best-effort** — a failure is logged and swallowed so it never
breaks the flow that triggered it — and the body NEVER carries Restricted data (chat text,
coordinates, identity numbers), only a neutral prompt.

Wired events:
- **New order** → a radar ping to every active, verified courier in the order's city
  (the broadcast deferred from Phase 6).
- **New chat message** → a "New message" push to the recipient, with no message text.

## Background jobs (SPEC SECTION 21)
Scheduled TaskIQ tasks, each Redis-locked so only one worker runs a sweep at a time:
- `reconcile_ledger` (nightly) — the money invariants.
- `deliver_pending_receipts` (5 min) — the paid-invoice receipt sweep.
- `run_auto_approve` (15 min) — auto-approve overdue deliveries.
- `run_expire_stale` (10 min) — expire lapsed unpaid invoices (release the held wallet
  funds, expire the open gateway intent, return the promo, reopen the order to ASSIGNED)
  and stale wallet-top-up intents.
- `run_purge_refresh_tokens` (nightly) — delete refresh tokens expired longer than
  `REFRESH_TOKEN_RETENTION_DAYS` (default 30), keeping a forensic window for
  reuse-detection while bounding table growth.

## Key rotation (SPEC SECTION 17.1)
`uv run python -m app.rotate_keys` re-encrypts the **mutable** Restricted columns
(`courier_profiles.national_id`/`passport_id`, `conversations.last_message_preview`,
`withdrawals.iban`) to the active `FIELD_ENCRYPTION_KEY_VERSION`, preserving each column's
AAD. `messages.content` is append-only (a DB trigger forbids updating it) and is NEVER
rotated, so its write-time key versions must stay in `FIELD_ENCRYPTION_KEYS` while any
message references them. New writes already use the active version, so rotation only ever
shrinks the set of old versions still in use by the mutable columns.
