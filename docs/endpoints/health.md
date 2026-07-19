# Health

## GET /api/health
Liveness probe. **Auth**: none. No dependency checks (so an orchestrator never restarts
the container over a transient Redis blip).

### Success 200
```json
{ "status": "ok" }
```

## GET /api/health/ready
Readiness probe. **Auth**: none. Actively checks the backing services — a `SELECT 1`
on the database and a `PING` on Redis — and reports each. Never throttled. The probe
never raises, so a dependency outage is a clean **503**, not a 500. A transient 503 does
not imply the process itself is unhealthy (that is what `/api/health` is for).

### Success 200 — all dependencies up
```json
{ "status": "ready", "checks": { "database": "ok", "redis": "ok" } }
```

### 503 — a dependency is down
```json
{ "status": "unavailable", "checks": { "database": "ok", "redis": "down" } }
```

---

The remaining endpoint groups (`auth`, `users`, `couriers`, `media`, `orders`,
`invoices`, `promos`, `payments`, `wallets`, `chat`, `admin`) are documented as each
group's implementation phase lands (SPEC SECTION 25). Every endpoint doc follows the
template in the master spec §4.4: auth, role, required state, path/body params, a full
success example, and every error `code` with a UI hint.
