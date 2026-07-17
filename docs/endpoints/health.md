# Health

## GET /api/health
Liveness probe. **Auth**: none. No dependency checks (so an orchestrator never restarts
the container over a transient Redis blip).

### Success 200
```json
{ "status": "ok" }
```

## GET /api/health/ready
Readiness probe. **Auth**: none. Reports whether backing services (DB, Redis, S3) are
reachable; may fail transiently without implying the process is unhealthy.

### Success 200
```json
{ "status": "ready" }
```

---

The remaining endpoint groups (`auth`, `users`, `couriers`, `media`, `orders`,
`invoices`, `promos`, `payments`, `wallets`, `chat`, `admin`) are documented as each
group's implementation phase lands (SPEC SECTION 25). Every endpoint doc follows the
template in the master spec §4.4: auth, role, required state, path/body params, a full
success example, and every error `code` with a UI hint.
