# Wallets

Money is always a decimal STRING (e.g. `"300.00"`). Reads are scoped to the caller's
own wallet — the actor comes from the JWT, never a path or body value.

## GET /api/wallets/me
Return the caller's wallet snapshot. **Auth**: Bearer JWT · **Role**: CUSTOMER or COURIER.
### Success 200 — WalletResponse
```json
{ "balance": "300.00", "held_balance": "0.00", "available": "300.00", "currency": "SAR" }
```
`available = balance - held_balance`; a held balance is funds reserved by an escrow hold.
### Errors
| status | code | when |
| 403 | FORBIDDEN | the caller's role may not hold a wallet |
| 404 | NOT_FOUND | no wallet for this user |

## GET /api/wallets/me/transactions
Return the caller's ledger entries, newest first (keyset paged). **Auth**: Bearer JWT.
### Query
| name | type | default | notes |
| cursor | string | — | pass the previous page's `next_cursor` |
| limit | int | 20 | 1–100 |
### Success 200 — TransactionPage
```json
{
  "items": [
    { "id": "uuid", "amount": "300.00", "type": "TOPUP", "status": "SETTLED",
      "balance_after": "300.00", "created_at": "2026-09-12T14:30:00Z" }
  ],
  "next_cursor": null
}
```
`amount` is signed (`+` credit, `−` debit). `next_cursor` is null on the last page.

## POST /api/wallets/topup
Start a wallet top-up (gateway-funded). See `docs/endpoints/payments.md`.
