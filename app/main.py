"""FastAPI application factory (SPEC SECTION 3, 5, 17.2 A05).

Wires configuration, logging, middleware, security headers, CORS, the global
exception handler, and routers. Docs are enabled only in development; CORS is
unrestricted only in development; dev-only routes are registered only in development
(interlock layer 4).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.core.config import Settings, get_settings
from app.core.db import build_engine, build_session_factory
from app.core.jwt import JwtError, decode_access_token
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware, error_response, register_exception_handlers
from app.core.ratelimit import RateLimiter
from app.core.redis import build_redis
from app.integrations.factory import build_clients
from app.routers import health

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}

# A restrictive CSP for the admin surface (SPEC SECTION 18.4): no inline scripts,
# no framing, self-only sources.
_ADMIN_CSP = (
    "default-src 'self'; script-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the configured FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL)

    app = FastAPI(
        title="SAFE-GIFT API",
        version="0.1.0",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    # Constructing clients at boot triggers the §5.2 interlock (raises on violation).
    app.state.clients = build_clients(settings)
    # Shared async engine/session factory and Redis client, built once per app.
    engine = build_engine(settings)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    app.state.redis = build_redis(settings)

    _install_middleware(app, settings)
    register_exception_handlers(app)
    _install_shutdown(app)
    app.include_router(health.router)

    from app.routers import (
        admin_api,
        auth,
        chat,
        devices,
        invoices,
        media,
        orders,
        promos,
        ratings,
        users,
        wallets,
        webhooks,
    )

    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(wallets.router)
    app.include_router(media.router)
    app.include_router(orders.router)
    app.include_router(invoices.router)
    app.include_router(promos.router)
    app.include_router(webhooks.router)
    app.include_router(ratings.router)
    app.include_router(admin_api.router)
    app.include_router(chat.router)
    app.include_router(devices.router)

    if settings.ADMIN_DASHBOARD_ENABLED:
        _register_admin(app)

    if settings.ENVIRONMENT.value == "development":
        _register_dev_routes(app)

    return app


def _client_identity(request: Request, settings: Settings) -> str:
    """Derive a throttle key: the authenticated user id, else the client IP.

    A best-effort token decode (no denylist check — that is auth's job) lets an
    authenticated caller be limited by identity rather than a shared NAT address.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token = header[len("Bearer ") :].strip()
        try:
            claims = decode_access_token(settings, token)
        except JwtError:
            pass
        else:
            return f"user:{claims.sub}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    """Install the security headers, CORS, request id, rate limiter, and body-size guard.

    Middleware added later wraps earlier ones, so these calls run in reverse at request
    time. The intended request-time order is: security-header stamp (outermost, so every
    response — even a 429 or 413 — is stamped and de-fingerprinted), CORS, request-id
    (bound before any envelope is built), then the rate limiter and body-size guard,
    which short-circuit before a route or the database is ever touched.
    """
    _install_request_guards(app, settings)
    app.add_middleware(RequestIdMiddleware)
    _install_cors(app, settings)
    _install_security_headers(app)


def _guard_body_size(request: Request, settings: Settings) -> Response | None:
    """Return a rejection response for an oversized or undeclared body, else None.

    A declared ``Content-Length`` over the cap is a 413. A body sent with chunked
    transfer encoding declares no length at all and would stream past the check
    (audit SEC-7) — the JSON API never needs chunked uploads (media bytes go straight
    to S3), so those requests are rejected with 411 Length Required.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            length = int(content_length)
        except ValueError:
            length = 0
        if length > settings.MAX_REQUEST_BODY_BYTES:
            return error_response(413, "PAYLOAD_TOO_LARGE", "The request body is too large.")
    elif "chunked" in request.headers.get("transfer-encoding", "").lower():
        return error_response(411, "LENGTH_REQUIRED", "Requests must declare a Content-Length.")
    return None


def _install_request_guards(app: FastAPI, settings: Settings) -> None:
    """One middleware for both request guards (audit PERF-4): body size, then throttle."""

    @app.middleware("http")
    async def _request_guards(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rejected = _guard_body_size(request, settings)
        if rejected is not None:
            return rejected
        # Health probes and CORS preflight are never throttled.
        if (
            not settings.RATE_LIMIT_ENABLED
            or request.method == "OPTIONS"
            or request.url.path.startswith("/api/health")
        ):
            return await call_next(request)
        limiter = RateLimiter(
            request.app.state.redis,
            max_requests=settings.RATE_LIMIT_MAX_REQUESTS,
            window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
        )
        decision = await limiter.check(_client_identity(request, settings))
        if not decision.allowed:
            return error_response(
                429,
                "RATE_LIMITED",
                "Too many requests. Please try again later.",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
        return await call_next(request)


def _install_cors(app: FastAPI, settings: Settings) -> None:
    """Add CORS: wildcard without credentials in development, the allow-list in production."""
    if settings.ENVIRONMENT.value == "development":
        origins = ["*"]
        allow_credentials = False  # never wildcard origins with credentials.
    else:
        origins = settings.cors_origins
        allow_credentials = True
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _install_security_headers(app: FastAPI) -> None:
    """Stamp security headers and strip fingerprinting headers on every response.

    Added last so it is the outermost layer: even a rate-limit 429 or an oversized-body
    413 rejected upstream gets the security headers and the fingerprint stripping.
    """

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers[key] = value
        # The admin surface gets a strict CSP on top of the shared headers.
        if request.url.path.startswith("/admin"):
            response.headers["Content-Security-Policy"] = _ADMIN_CSP
        # Strip fingerprinting headers (SPEC SECTION 17.2 A05).
        for header in ("Server", "X-Powered-By"):
            if header in response.headers:
                del response.headers[header]
        return response


def _install_shutdown(app: FastAPI) -> None:
    """Close pooled resources on shutdown: HTTP clients, Redis, and the engine.

    The real integration clients hold long-lived httpx pools (audit PERF-1); a clean
    shutdown returns their connections. Fakes have no ``aclose`` and are skipped.
    """

    @app.on_event("shutdown")
    async def _close_resources() -> None:
        clients = app.state.clients
        for client in (clients.gateway, clients.email, clients.sms, clients.push):
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()
        await app.state.redis.aclose()
        await app.state.engine.dispose()


def _register_admin(app: FastAPI) -> None:
    """Mount the server-rendered admin dashboard, its static files, and redirect handler."""
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    from app.admin.deps import AdminRedirect
    from app.admin.router import router as admin_router

    @app.exception_handler(AdminRedirect)
    async def _handle_admin_redirect(request: Request, exc: AdminRedirect) -> RedirectResponse:
        return RedirectResponse(exc.location, status_code=303)

    app.include_router(admin_router)
    static_dir = Path(__file__).parent / "admin" / "static"
    app.mount("/admin/static", StaticFiles(directory=str(static_dir)), name="admin-static")


def _register_dev_routes(app: FastAPI) -> None:
    """Register development-only routes (interlock layer 4)."""
    from app.routers import dev

    app.include_router(dev.router)
