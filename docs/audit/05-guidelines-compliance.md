# Guidelines-compliance audit (current state, 2026-07-21)

CLAUDE.md's hard rules, checked rule by rule against the code actually in the tree.

## The hard rules

| Rule | Verdict | Evidence |
| --- | --- | --- |
| `uv` only, never `pip` | ✅ Pass | Dockerfile installs uv from its published image ("pip is banned"); CI uses `uv sync --frozen` / `uvx`; no `requirements.txt`, poetry, or pipenv anywhere. |
| Money is `Decimal`, never `float` | ✅ Pass | `parse_money` rejects floats explicitly; wire format is strings; the only `float()` calls are coordinates/distance, which are not money. |
| Quantize only through `core/money.py` | ✅ Pass | `.quantize` appears only in `money.py`. |
| Pricing only in `core/pricing.py` | ✅ Pass | `calculate_invoice_totals` / `compute_settlement` are the only price builders. |
| No raw SQL strings / f-string SQL | ✅ Pass | Swept: zero f-string SQL; only `:named`-param `text()` in the promo repo and the readiness `SELECT 1`. |
| Ownership enforced in the query | ✅ Pass | `get_for_actor` pattern throughout; actor id from the JWT everywhere. |
| Ledger append-only | ✅ Pass | Trigger-enforced; corrections are compensating entries. |
| Secrets from env, `SecretStr`, never logged/baked | ✅ Pass | All secrets (including the new `OTP_HMAC_KEY`) are `SecretStr`; CI asserts a secret-free image. |
| No healthcare data | ✅ Pass | No such fields, models, or inference anywhere. |
| Base path `/api`, no version prefix | ✅ Pass | Every router uses `/api/...`. |
| Layering `routers → services → repositories → models` | ✅ Pass* | No service imports FastAPI (swept). Routers construct repositories only as DI wiring; `dev.py`'s single read stays dev-only. |
| `ST_MakePoint` longitude first | ✅ Pass | All call sites lng-first. |
| `ST_Distance` on `geography` for metres | ✅ Pass | Both operands cast. |
| Redis lock release via Lua compare-and-delete | ✅ **Pass (fixed)** | Previously partial (SEC-5): all five scheduled jobs now use `core/locks.redis_lock`; the plain-`DEL` pattern is gone from the codebase. |
| Sync SDK in async → `run_in_threadpool` | ✅ Pass | No sync SDK exists; every real client is async httpx/aioboto3 (now pooled). |
| Webhook signature over the raw body | ✅ Pass | `request.body()` bytes go straight to verification; the IP gate added by SEC-2 runs before the body is even read. |
| Conventional Commits | ✅ Pass | Full history conforms. |
| Google-style docstrings (ruff D) | ✅ Pass | Enforced by CI; ruff is clean. |
| Docs updated with every contract move | ✅ Pass | The fix batch updated conventions (411, WS guards), chat/payments/devices endpoint docs, and `.env.example`; CI fails on a stale OpenAPI spec. |
| Tests for every behaviour change | ✅ Pass | 217 tests, 87.9 % coverage against an 85 % CI gate; every audit fix that changed behaviour carries a test. |
| Never commit a secret / real `.env` | ✅ Pass | `.env.example` is all placeholders; secret-scanning job in CI. |
| Naming conventions (SPEC §7) | ✅ Pass | Plural snake_case tables, UUID `id` PKs, `_amount`/`_balance`/`_encrypted` suffixes. |

\* One dev-only exception, noted in place.

## Config hygiene (re-checked)

- `PAYLINK_ALLOWED_IPS` is enforced at the webhook and the OTP HMAC key chain ends on a
  validated secret in every algorithm mode. Every tunable is an env var with a safe
  default, documented in `.env.example` (`OTP_HMAC_KEY`, `REFRESH_TOKEN_RETENTION_DAYS`,
  `RATE_LIMIT_*`, `MAX_REQUEST_BODY_BYTES`, `WS_*`).
- **One caveat, not a rule breach (NF-1)**: the webhook IP allowlist and per-IP rate
  limiting read the peer address, which behind a proxy is the LB, not the origin. The
  code keeps its promise *at the socket*; making it keep it *at the origin* is a deploy
  requirement (`--forwarded-allow-ips`), documented in the README. Not counted against a
  hard rule, but flagged so the allowlist isn't trusted blindly in a proxied deployment.
- **One dead config item (NF-2)**: `MIN_WITHDRAWAL_AMOUNT` is defined and read nowhere,
  because the withdrawal flow is unbuilt. This is the inverse of the closed first-pass
  finding (config promising an unenforced control) — here a control knob has no feature
  to govern. Resolve with the NF-2 scope decision.
- The boot interlock's promises were re-swept: everything `config.py` validates is
  genuinely enforced somewhere, and nothing enforced is unvalidated.

## Summary

22 hard rules checked: **22 pass.** No rule is violated by the current tree. The two
config-hygiene notes above (NF-1 deploy caveat, NF-2 dead knob) are tracked as findings
rather than rule breaches — one is a deployment requirement, the other rides on the
withdrawal-scope decision.
