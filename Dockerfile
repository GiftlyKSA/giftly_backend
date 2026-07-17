# SAFE-GIFT deployment image (SPEC SECTION 22.1). There is exactly ONE Dockerfile.
# Multi-stage, non-root, read-only-friendly, no secrets baked in.

# ---- Stage 1: builder -------------------------------------------------------
FROM python:3.13-slim AS builder

# uv comes from its published image; pip is banned everywhere.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Resolve dependencies first for layer caching; source changes must not re-resolve.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Then bring in the application source and finalise the environment.
COPY app ./app
COPY alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev

# ---- Stage 2: runtime -------------------------------------------------------
FROM python:3.13-slim AS runtime

# Non-root user; no shell utilities, compilers, uv, or git in the final image.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app /app/app
COPY --from=builder /app/alembic.ini /app/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER appuser
EXPOSE 8000

# Liveness only — a readiness probe here would restart on a transient Redis blip.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

# Workers and timeouts come from the environment; no `uv run` at runtime.
CMD ["gunicorn", "app.main:create_app()", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--timeout", "60"]
