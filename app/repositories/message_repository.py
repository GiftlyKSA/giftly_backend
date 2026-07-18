"""Minimal message writer for system messages (SPEC SECTION 20.C).

The full chat repository lands in Phase 11; the order flow only needs to append an
encrypted SYSTEM message when an order is assigned. ``messages`` is append-only.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message
from app.models.enums import MessageType


class MessageWriter:
    """Appends messages and maintains the conversation preview/counters."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the writer to a session."""
        self._session = session

    async def add_system_message(
        self,
        *,
        conversation_id: uuid.UUID,
        sender_id: uuid.UUID,
        content_encrypted: str,
        message_type: MessageType,
    ) -> Message:
        """Append a message and bump the conversation's last-message timestamp."""
        message = Message(
            conversation_id=conversation_id,
            sender_id=sender_id,
            message_type=message_type,
            content_encrypted=content_encrypted,
        )
        self._session.add(message)
        conversation = await self._session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.last_message_timestamp = (
                message.created_at or conversation.last_message_timestamp
            )
        await self._session.flush()
        return message
