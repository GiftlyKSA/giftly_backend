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
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware, register_exception_handlers
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
    app.include_router(health.router)

    if settings.ADMIN_DASHBOARD_ENABLED:
        _register_admin(app)

    if settings.ENVIRONMENT.value == "development":
        _register_dev_routes(app)

    return app


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    """Install security headers, request-id correlation, and CORS."""

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

    app.add_middleware(RequestIdMiddleware)

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
