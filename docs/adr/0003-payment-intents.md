# ADR 0003 — A single payment_intents record unifies the gateway

## Status
Accepted (2026-07-17)

## Context
There are two reasons to call Paylink: an invoice remainder and a wallet top-up. The
webhook must know, from a transaction number alone, which one it is settling.

## Decision
A single `payment_intents` table is the only gateway-facing record, discriminated by a
`purpose` enum (`ORDER_INVOICE` | `WALLET_TOPUP`). The webhook does one lookup by
`paylink_transaction_no` and dispatches on `purpose`.

## Consequences
- No trial-and-error lookup across two tables — the ambiguity that produces
  double-credits is designed out.
- The webhook verifies the amount against `payment_intents.amount` and refuses to settle
  on a mismatch.
- Idempotency is enforced by a Redis lock on the transaction number plus a unique
  `transactions.idempotency_key`.
