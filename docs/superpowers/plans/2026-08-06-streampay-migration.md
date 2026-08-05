# StreamPay Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Paylink with StreamPay for wallet top-ups and order-invoice payment links.

**Architecture:** Keep the existing payment-intent and ledger settlement model. Replace the provider adapter with a StreamPay client that creates a consumer, one-time products, and a hosted payment link; store the Stream payment-link ID and settle signed Stream webhooks idempotently.

**Tech Stack:** FastAPI, SQLAlchemy/Alembic, httpx, Pydantic, pytest, Redis.

## Global Constraints

- Preserve all existing wallet, invoice, escrow, and idempotency safeguards.
- Use StreamPay hosted checkout; Apple Pay is provided there after merchant activation.
- Keep provider credentials server-side and verify webhook signatures over the exact raw body.
- Remove Paylink runtime code, routes, configuration, schema names, and documentation.

---

### Task 1: Define and test the StreamPay adapter

**Files:**
- Create: `app/integrations/streampay/base.py`, `app/integrations/streampay/real.py`, `app/integrations/streampay/fake.py`
- Modify: `tests/unit/test_integrations_extra.py`

- [ ] Write failing adapter tests for Base64 `x-api-key` auth, consumer/product/link creation, and timestamped webhook HMAC verification.
- [ ] Run the focused test and confirm it fails because StreamPay classes do not exist.
- [ ] Implement only the adapter contract and client behavior required by those tests.
- [ ] Run the focused test and confirm it passes.

### Task 2: Wire StreamPay into payment orchestration and persistence

**Files:**
- Modify: `app/integrations/factory.py`, `app/services/payment_service.py`, `app/repositories/payment_repository.py`, `app/models/tables.py`, `app/routers/webhooks.py`, `app/routers/dev.py`, `app/schemas/payments.py`
- Create: `app/migrations/versions/<revision>_replace_paylink_with_streampay.py`
- Modify: `tests/integration/test_payment_service.py`, `tests/integration/test_payments_api.py`, `tests/integration/test_fulfillment_api.py`, `tests/integration/test_expiry_worker.py`

- [ ] Update the focused tests to name Stream payment-link fields and produce a signed Stream-shaped event.
- [ ] Run focused tests and confirm they fail against the Paylink flow.
- [ ] Implement Stream link creation and webhook settlement without changing ledger behavior.
- [ ] Add the forward/reverse Alembic migration for renamed intent fields and partial index.
- [ ] Run focused tests and confirm they pass.

### Task 3: Replace deployment configuration and documentation

**Files:**
- Modify: `app/core/config.py`, `.env.example`, `README.md`, `docs/endpoints/payments.md`, `docs/api-4-ui.md`, `docs/adr/0003-payment-intents.md`, `docs/audit/*`
- Modify: `tests/unit/test_config.py`, `tests/unit/test_interlock.py`

- [ ] Replace all Paylink settings with production-required StreamPay credentials and webhook secret.
- [ ] Document the hosted payment-link flow, the webhook setup, and Apple Pay activation/domain-association requirements.
- [ ] Update audit findings that refer to Paylink.
- [ ] Run focused configuration tests and confirm they pass.

### Task 4: Verify and publish

**Files:**
- Modify: `docs/openapi.json` if the generated schema changes

- [ ] Run formatting/lint/type checks defined by the repository.
- [ ] Run payment-focused tests and the full suite with a disposable test environment.
- [ ] Inspect the final diff and ensure no runtime Paylink references remain.
- [ ] Commit the migration and push the verified commit directly to `main` as requested.
