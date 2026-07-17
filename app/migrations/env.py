"""Alembic environment for SAFE-GIFT (async engine, URL from Settings).

The database URL is read from the application Settings so no secret lives in
alembic.ini. Autogenerate compares against ``Base.metadata``; every generated
revision is a DRAFT to be read line by line before committing (SPEC SECTION 4.8).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# PostGIS manages these tables/indexes itself; exclude them from autogenerate so a
# diff never proposes dropping spatial_ref_sys (SPEC SECTION 4.8 — clean revisions).
_POSTGIS_TABLES = {"spatial_ref_sys"}


def _include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Exclude PostGIS-managed objects from autogenerate comparison."""
    if type_ == "table" and name in _POSTGIS_TABLES:
        return False
    return True


_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.DATABASE_URL.get_secret_value())


def run_migrations_offline() -> None:
    """Run migrations in offline mode, emitting SQL against a URL."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in online mode against the async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
