"""SQLAlchemy declarative base and shared column mixins (SPEC SECTION 10).

Every table has a UUID ``id`` and ``created_at`` / ``updated_at`` TIMESTAMPTZ in
UTC; ``updated_at`` is maintained by a DB trigger (see the baseline migration).
Soft-deletable tables add ``deleted_at``. ORM models carry no business logic beyond
hybrid properties (SPEC SECTION 3).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class UUIDPrimaryKeyMixin:
    """Adds the mandated UUID ``id`` primary key defaulted by the database."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` TIMESTAMPTZ, both defaulted server-side."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SoftDeleteMixin:
    """Adds a nullable ``deleted_at`` for soft-deletable tables."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
