"""Database fixtures for integration tests.

Each test runs inside an outer transaction that is always rolled back, so tests never
pollute each other and the schema created by ``alembic upgrade head`` is reused. If the
database is unreachable the whole module is skipped (CI always has a real Postgres).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.db import build_engine
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from tests.conftest import make_test_settings


@pytest_asyncio.fixture
async def db_connection() -> AsyncIterator[AsyncConnection]:
    """Yield a connection wrapped in a transaction that is rolled back after the test."""
    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    settings = make_test_settings(**overrides)
    engine = build_engine(settings)
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001 — no DB available; skip DB-backed tests.
        await engine.dispose()
        pytest.skip(f"database unavailable: {exc}")
    trans = await conn.begin()
    try:
        yield conn
    finally:
        # A DB error may have already aborted the transaction driver-side; only roll
        # back if it is still active to avoid a spurious warning.
        if trans.is_active:
            await trans.rollback()
        await conn.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Yield a session bound to the rolled-back connection."""
    async with AsyncSession(bind=db_connection, expire_on_commit=False) as session:
        yield session
