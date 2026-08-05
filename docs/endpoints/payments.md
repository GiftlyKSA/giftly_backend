# Payments

Wallet top-ups and invoice remainders each use one `payment_intents` record (ADR 0003).
StreamPay creates a consumer, one-time products, and a hosted payment link. All money is
sent and returned as decimal strings.

## POST /api/wallets/topup

Start a wallet top-up. **Auth:** Bearer JWT. **Role:** CUSTOMER or COURIER.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| amount | string | yes | 100.00-20000.00 |

Success `201`: `{ payment_intent_id, amount, payment_url }`. Redirect the client to the
returned hosted `payment_url`; the wallet is credited only after StreamPay confirms it.

## POST /api/invoices/{invoice_id}/pay

Pay an issued invoice from the wallet, StreamPay, or both. The wallet is applied first.
When a remainder is due, it is held and the response is `PENDING` with `payment_url`.

StreamPay receives the frozen invoice items where they can exactly represent the payable
total, plus a visible adjustment for delivery, service fees, tax, and discounts. Split
payments use one authoritative outstanding-balance item so StreamPay's total always
matches the ledger amount exactly.

## POST /api/webhooks/streampay

StreamPay's public callback; no JWT is used. It requires:

- `X-Webhook-Signature: t=<timestamp>,v1=<HMAC-SHA256>`
- an HMAC over the exact raw body as `timestamp.raw_body`

The service locks the StreamPay payment-link ID, verifies status and amount, and settles
idempotently. A duplicate delivery is a no-op.

Expected JSON shape:

```json
{
  "event_type": "PAYMENT_SUCCEEDED",
  "data": {
    "payment_link": {"id": "<stream-payment-link-id>"},
    "payment": {"status": "PAID", "amount": "724.50"}
  }
}
```

Success `200`: `{ outcome }`, where outcome is `processed`, `already_processed`, or
`failed`.

## POST /api/dev/streampay/simulate

Development only. Submit `{ "payment_link_id": "..." }` to fire a correctly signed
Stream-shaped webhook at the real handler. This route is absent in test and production.

## Apple Pay

`payment_url` is StreamPay's hosted checkout URL. StreamPay presents Apple Pay when it is
enabled for the merchant and available on the payer's device; no API key ever reaches the
client. For future embedded checkout, host StreamPay's exact merchant-domain association
file at `/.well-known/apple-developer-merchantid-domain-association` and complete their
merchant registration first.

## Escrow model

An invoice total lands in `SYSTEM_ESCROW`, sourced by the wallet and StreamPay payment.
It is released only through the existing delivery and approval flow.
