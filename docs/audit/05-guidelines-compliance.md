# Guidelines-compliance audit

CLAUDE.md's hard rules, checked rule by rule against the code actually in the tree.

## The hard rules

| Rule | Verdict | Evidence |
| --- | --- | --- |
| `uv` only, never `pip` | ✅ Pass | Dockerfile installs uv from its published image and comments "pip is banned" (`Dockerfile:7-8`); CI uses `uv sync --frozen` / `uvx`; no `requirements.txt`, poetry, or pipenv anywhere. |
| Money is `Decimal`, never `float` | ✅ Pass | `parse_money` **rejects** floats explicitly (`app/core/money.py:42-43`); wire format is strings; the only `float()` calls in the app are coordinates/distance (`order_repository.py:196,217`), which are not money. |
| Quantize only through `core/money.py` | ✅ Pass | `.quantize` appears only in `money.py`; every service imports `quantize_money`. |
| Pricing only in `core/pricing.py` | ✅ Pass | `calculate_invoice_totals` / `compute_settlement` are the only price builders; invoice service, preview, receipts, and admin all consume the persisted result. |
| No raw SQL strings / f-string SQL | ✅ Pass | Swept: zero f-string SQL. The only `text()` outside migrations is the promo atomic claim/release with `:named` params (`promo_repository.py:93-120`) and the readiness `SELECT 1`. |
| Ownership enforced in the query | ✅ Pass | The `get_for_actor` pattern is used across invoice/order/chat/device repos (e.g. `device_token_repository.remove` deletes `WHERE user_id = :actor AND token = :token`); no fetch-then-compare was found. Actor id comes from the JWT everywhere (`app/core/deps.py`). |
| Ledger append-only | ✅ Pass | Trigger-enforced (baseline migration :86-99): DELETE forbidden, PENDING→SETTLED\|REVERSED only, core columns immutable. Corrections are compensating entries (dispute flows). |
| Secrets from env, `SecretStr`, never logged/baked | ✅ Pass | All secret settings are `SecretStr` (`config.py`); logging scrubber in place; CI asserts no secret in image history. |
| No healthcare data | ✅ Pass | No such fields, models, or inference anywhere. |
| Base path `/api`, no version prefix | ✅ Pass | Every router uses `/api/...`; no `/api/v1` exists (swept). |
| Layering `routers → services → repositories → models` | ✅ Pass* | No service imports FastAPI (swept — zero hits). Routers construct repositories only to inject into services (DI wiring), and `dev.py` does one read via `PaymentRepository` to build a simulated webhook — dev-only, acceptable, noted. |
| `ST_MakePoint` longitude first | ✅ Pass | All three call sites lng-first (`order_repository.py:60,89,211`). |
| `ST_Distance` on `geography` for metres | ✅ Pass | Both operands cast (`order_repository.py:214`). |
| Redis lock release via Lua compare-and-delete | ⚠️ **Partial** | `core/locks.py` implements it and the webhook path uses it — but all four scheduled workers hand-roll `SET NX EX` + plain `DEL` (see SEC-5). The rule is violated by the codebase's own workers. |
| Sync SDK in async → `run_in_threadpool` | ✅ Pass | No sync SDK exists; every real client is async httpx/aioboto3. |
| Webhook signature over the raw body | ✅ Pass | `request.body()` bytes go straight to `verify_webhook_signature` (`webhooks.py:28,40`); the dev simulate route signs the same raw bytes. |
| Conventional Commits | ✅ Pass | Full history conforms (`feat|fix|ci|docs` etc.). |
| Google-style docstrings (ruff D) | ✅ Pass | Enforced by CI; ruff is clean. |
| Docs updated with every contract move | ✅ Pass | Every phase shipped endpoint docs + regenerated `openapi.json`; CI fails on a stale spec. |
| Tests for every behaviour change | ✅ Pass | 210 tests, 87.9 % coverage against an 85 % CI gate. |
| Never commit a secret / real `.env` | ✅ Pass | `.env.example` is all placeholders; secret-scanning job in CI. |
| Naming conventions (SPEC §7) | ✅ Pass | Plural snake_case tables, UUID `id` PKs, `_amount`/`_balance` money suffixes, `_encrypted` suffixes — spot-checked across `tables.py`. |

\* One deviation and one dev-only exception, both noted in place.

## Config hygiene findings

- **Dead config**: `PAYLINK_ALLOWED_IPS` is validated as required in production but
  never read again (SEC-2). Config that promises unenforced controls should be wired
  or removed.
- **Cross-mode secret reuse**: the OTP HMAC borrows `JWT_SECRET` and silently degrades
  under RS256 (SEC-3). Rule-of-thumb worth adopting: every secret has exactly one
  purpose, and the boot validator covers every algorithm combination.
- Everything else in `config.py`'s interlock is genuinely enforced and tested
  (DEBUG/docs/CORS/fake-gating all have assertions in the suite).

## Summary

22 rules checked: **21 pass, 1 partial** (worker lock release — SEC-5, a
four-file mechanical fix using the existing `redis_lock` helper). The codebase
practises what its CLAUDE.md preaches to an unusually high degree; the two config
findings are about promises the config makes that the code doesn't keep, which is the
same failure class in the opposite direction.
