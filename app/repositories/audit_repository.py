"""Audit-log persistence (SPEC SECTION 8.19).

Every security-relevant and admin action writes one append-only row here. Metadata is
scrubbed of Restricted data before it reaches this layer.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


class AuditRepository:
    """Reads and appends audit-log rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def record(
        self,
        *,
        actor_user_id: uuid.UUID | None,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None,
        ip_address: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> AuditLog:
        """Append one audit row and flush it."""
        row = AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            ip_address=ip_address,
            audit_metadata=metadata,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_recent(
        self,
        *,
        actor_user_id: uuid.UUID | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
    ) -> list[AuditLog]:
        """Return recent audit rows, newest first, with optional filters."""
        query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        if actor_user_id is not None:
            query = query.where(AuditLog.actor_user_id == actor_user_id)
        if action is not None:
            query = query.where(AuditLog.action == action)
        if entity_type is not None:
            query = query.where(AuditLog.entity_type == entity_type)
        return list(await self._session.scalars(query))
