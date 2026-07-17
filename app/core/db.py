"""Async database engine and session factory (SPEC SECTION 3, 16.6).

Uses SQLAlchemy 2.x async with asyncpg. Driver-side prepared-statement caching is
disabled because PgBouncer runs in transaction mode, where a pooled connection may
serve different backends across statements and a cached plan can bind to the wrong
one. Sessions never rely on session-level state across requests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    """Create the async engine for the configured database URL."""
    return create_async_engine(
        settings.DATABASE_URL.get_secret_value(),
        echo=False if settings.is_production else settings.DEBUG,
        pool_pre_ping=True,
        # PgBouncer transaction mode: no server-side statement cache.
        connect_args={"statement_cache_size": 0},
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session, committing on success and rolling back on error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
