# ADR 0005 — The platform funds promos, and does not discount its own fee

## Status
Accepted (2026-07-17)

## Context
A promo discount reduces what the customer pays. Two questions follow: is the service
fee discounted, and who absorbs the discount at payout?

## Decision
1. The service fee is NOT part of the discountable base — the promo subsidises goods and
   craft, not the platform's own fee (discounting it would double-count the subsidy).
2. Courier payout is computed on the PRE-discount base (`items_net + courier_fee_net`)
   minus commission. The platform funds the promo, so the courier is never silently
   underpaid because marketing ran a campaign.

## Consequences
- `revenue = total − tax − courier_payout` naturally equals
  `service_fee + commission − promo_subsidy`, and can go negative when subsidies exceed
  fees — which is why `SYSTEM_REVENUE` is excluded from the non-negative balance CHECK.
