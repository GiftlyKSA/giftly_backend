# Payments

There are exactly two reasons to call the gateway — a wallet top-up and an invoice
remainder — both unified through one `payment_intents` record (ADR 0003). The webhook
verifies the HMAC over the **raw** body, does ONE lookup by transaction number, and
dispatches on `purpose`. All money is a decimal STRING.

## POST /api/wallets/topup
Start a wallet top-up. **Auth**: Bearer JWT · **Role**: CUSTOMER or COURIER.
### Body
| field | type | required | notes |
| amount | string | yes | 100.00–20000.00 |
### Success 201 — TopupResponse
`{ payment_intent_id, amount, payment_url }` — redirect the client to `payment_url`.
The wallet is credited only when the gateway webhook confirms the payment.
### Errors
| status | code | when |
| 404 | NOT_FOUND | you have no wallet |
| 422 | VALIDATION_ERROR | amount outside the permitted bounds |

## POST /api/invoices/{invoice_id}/pay
Pay an issued invoice from wallet, gateway, or a split. **Role**: CUSTOMER (the order's
customer). Available wallet balance is applied first; any remainder is charged to the
gateway.
### Success 200 — PayInvoiceResponse
| field | notes |
| status | `PAID` (wallet covered the total, settled now) or `PENDING` (gateway due) |
| amount_from_wallet / amount_from_gateway | the split |
| payment_url | set only when `status = PENDING` |

- **Wallet fully covers the total** → funds move into escrow immediately, the invoice is
  `PAID`, and the order advances to `IN_PROGRESS`.
- **A remainder is due** → the wallet portion is *held*, a gateway charge is created, and
  the webhook settles both portions into escrow on confirmation.

### Errors
| status | code | when |
| 404 | NOT_FOUND | no such invoice for you |
| 409 | CONFLICT | the invoice is not awaiting payment, or has expired |
| 409 | INVALID_STATE_TRANSITION | the order is not awaiting payment |
| 409 | INSUFFICIENT_FUNDS | a concurrent debit consumed the held balance |

## POST /api/webhooks/paylink
The gateway payment callback. **PUBLIC** — no JWT; authenticated by the
`X-Paylink-Signature` HMAC over the **raw** request body, plus a source-IP allowlist
(`PAYLINK_ALLOWED_IPS`, enforced when set — required in production) as defence in
depth. Settlement is idempotent at three layers: a Redis lock on the transaction
number, the intent's own status check, and the ledger's idempotency keys — a duplicate
delivery is a no-op.
### Body (raw JSON, signature verified before parsing)
`{ transaction_no, status, amount }`
### Success 200 — WebhookAck
`{ outcome }` — one of `processed`, `already_processed`, `failed`.
### Errors
| status | code | when |
| 403 | FORBIDDEN | the source IP is not on the allowlist |
| 401 | INVALID_SIGNATURE | the HMAC does not match the raw body |
| 400 | PAYMENT_AMOUNT_MISMATCH | the webhook amount != the intent amount |
| 404 | NOT_FOUND | no intent matches the transaction number |

## POST /api/dev/paylink/simulate  (development only)
Fire a correctly-signed webhook at the real handler for a transaction number — it never
bypasses webhook processing. **Registered only when `ENVIRONMENT=development`.**

## The escrow model
A paid invoice's total lands in `SYSTEM_ESCROW`, sourced from the customer's wallet and
the gateway (`-wallet -gateway +total == 0`). It is held there until delivery/approval
releases it to the courier and the platform (Phase 10). The promo subsidy is injected at
payout, not here — escrow holds exactly what the customer paid.
