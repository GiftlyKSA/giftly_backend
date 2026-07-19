# Flow: invoice-paid receipt

The system sends exactly **one** email — a receipt when an invoice is paid. There is no
HTTP endpoint; delivery is a background sweep.

## Mechanism

When an invoice becomes `PAID` (Phase 8) it enters the pending-receipt set, defined by the
partial index `idx_invoices_receipt_pending` (`status='PAID' AND receipt_email_sent_at IS
NULL`). A scheduled sweeper (`app/workers/receipts.py`, every 5 minutes, under a Redis
lock) drains that set:

1. Lock the invoice row `FOR UPDATE` and re-check it is still pending (at-most-once per
   pass — a second sweeper blocks, then sees the stamp and skips).
2. Look up the order's customer and their email.
   - **No email on file** → stamp `receipt_email_sent_at` and skip the send, so the
     invoice leaves the pending set instead of being retried forever.
3. Send the receipt template with amounts only (never a phone, coordinates, or any other
   Restricted data), then stamp `receipt_email_sent_at`.

Send-then-stamp makes delivery **at-least-once**: a crash between the send and the commit
retries on the next sweep (a receipt arriving twice is friendlier than never arriving). A
failed send for one invoice never blocks the rest — each is its own transaction.

## Template variables

`invoice_id`, `order_id`, `currency`, `items_net_amount`, `courier_fee_amount`,
`service_fee_amount`, `discount_amount`, `tax_amount`, `total_amount`, `promo_code`,
`paid_at`. The provider template key comes from `SNDR_INVOICE_PAID_TEMPLATE_KEY`
(default `invoice_paid_receipt`).

## Integrations

The email client is behind the `EmailClient` ABC. In production it is `SndrEmailClient`;
in development and test it is `FakeEmailClient`, which records sends in memory so the suite
asserts exactly one receipt on `PAID` and zero on every other event — with no network.
