# Enums

Every enum, its values, and the user-facing label the UI should show.

## user_role
`CUSTOMER` (Customer) · `COURIER` (Courier) · `ADMIN` (Admin)

## user_status
`ACTIVE` (Active) · `BANNED` (Banned) · `PENDING_VERIFICATION` (Pending verification)

## order_status
`NEW` (Open) · `ASSIGNED` (Courier assigned) · `WAITING_PAYMENT` (Awaiting payment) ·
`IN_PROGRESS` (In progress) · `DELIVERED` (Delivered — awaiting approval) ·
`COMPLETED` (Completed) · `CANCELLED` (Cancelled) · `DISPUTED` (In dispute) ·
`REFUNDED` (Refunded)

## invoice_status
`DRAFT` (Draft) · `ISSUED` (Awaiting payment) · `PAID` (Paid) · `CANCELLED` (Cancelled) ·
`EXPIRED` (Expired) · `REFUNDED` (Refunded)

## payment_purpose
`ORDER_INVOICE` (Order payment) · `WALLET_TOPUP` (Wallet top-up)

## payment_intent_status
`NEW` (Pending) · `PAID` (Paid) · `FAILED` (Failed) · `EXPIRED` (Expired) ·
`CANCELLED` (Cancelled)

## payment_method
`WALLET_ONLY` (Paid from wallet) · `GATEWAY_ONLY` (Paid by card/Apple Pay) ·
`SPLIT` (Wallet + card)

## promo_discount_type
`PERCENT` (Percentage) · `FIXED` (Fixed amount)

## promo_redemption_status
`RESERVED` (Reserved) · `CONSUMED` (Used) · `RELEASED` (Released)

## media_type
`CUSTOMER_REQUEST` (Request photo) · `DELIVERY_PROOF` (Delivery proof)

## message_type
`TEXT` (Text) · `IMAGE` (Image) · `VIDEO` (Video) · `SYSTEM` (System message)

## wallet_type
`CUSTOMER` · `COURIER` · `SYSTEM_ESCROW` · `SYSTEM_REVENUE` · `SYSTEM_GATEWAY` ·
`SYSTEM_TAX_PAYABLE` (the four SYSTEM_* wallets are internal and never shown to end users)

## transaction_type
`TOPUP` · `WITHDRAWAL` · `ESCROW_HOLD` · `ESCROW_RELEASE` · `PAYMENT` · `REFUND` ·
`COMMISSION` · `SERVICE_FEE` · `TAX` · `PROMO_SUBSIDY`

## transaction_status
`PENDING` (Pending) · `SETTLED` (Settled) · `REVERSED` (Reversed)

## device_os
`IOS` · `ANDROID`

## withdrawal_status
`REQUESTED` (Requested) · `APPROVED` (Approved) · `PAID` (Paid) · `REJECTED` (Rejected)

## dispute_status
`OPEN` (Open) · `RESOLVED_CUSTOMER` (Resolved for customer) ·
`RESOLVED_COURIER` (Resolved for courier) · `RESOLVED_SPLIT` (Resolved — split)
