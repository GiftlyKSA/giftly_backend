# ADR 0001 — Modular monolith, API-first

## Status
Accepted (2026-07-17)

## Context
SAFE-GIFT has clear domains (auth, orders, invoices, payments, wallets, chat, admin)
that share one transactional database and strong money invariants. We need speed of
delivery now and the option to extract services later.

## Decision
Build a modular monolith with strict layering (routers/admin → services → repositories
→ models) and module boundaries that only expose service interfaces. REST for state,
WebSockets for chat, server-rendered HTML for admin, background tasks for slow work.

## Consequences
- One deploy unit, one database, cross-module invariants (escrow, ledger) enforced in a
  single transaction — no distributed-transaction problem.
- The dependency rule keeps modules extractable: a module never reaches into another's
  tables, so pulling one into its own service later is a boundary change, not a rewrite.
- Discipline is required in review: a router with a raw query, or a service importing
  another module's repository, is a blocker.
