"""Chat persistence: conversations and messages (SPEC SECTION 10, 20, ADR 0004).

``messages`` is append-only (a trigger forbids UPDATE/DELETE except is_read/read_at) and
``content_encrypted`` is AES-256-GCM. The inbox preview is stored ENCRYPTED in
``conversations.last_message_preview_encrypted`` (ADR 0004), decrypted one row at a time.
Ownership is enforced in the query — a conversation is returned only to its two members.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message
from app.models.enums import MessageType


class ChatRepository:
    """Reads conversations and appends/reads/marks-read chat messages."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def get_for_actor(
        self, conversation_id: uuid.UUID, actor_id: uuid.UUID
    ) -> Conversation | None:
        """Return a conversation only if the actor is its customer or courier."""
        result: Conversation | None = await self._session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                (Conversation.customer_id == actor_id) | (Conversation.courier_id == actor_id),
            )
        )
        return result

    async def list_for_user(
        self, user_id: uuid.UUID, *, limit: int, before: tuple[datetime, uuid.UUID] | None
    ) -> list[Conversation]:
        """Return a user's conversations, most-recent activity first (keyset paged)."""
        query = select(Conversation).where(
            (Conversation.customer_id == user_id) | (Conversation.courier_id == user_id)
        )
        query = query.order_by(
            Conversation.last_message_timestamp.desc(), Conversation.id.desc()
        ).limit(limit)
        if before is not None:
            query = query.where(
                tuple_(Conversation.last_message_timestamp, Conversation.id) < before
            )
        return list(await self._session.scalars(query))

    async def add_message(
        self,
        *,
        conversation: Conversation,
        sender_id: uuid.UUID,
        content_encrypted: str,
        preview_encrypted: str,
        sender_is_customer: bool,
        message_type: MessageType = MessageType.TEXT,
    ) -> Message:
        """Append a message and update the conversation preview, timestamp, and unread.

        The recipient's unread counter is bumped (the sender's is untouched); the
        encrypted preview and last-message timestamp are refreshed.
        """
        message = Message(
            conversation_id=conversation.id,
            sender_id=sender_id,
            message_type=message_type,
            content_encrypted=content_encrypted,
        )
        self._session.add(message)
        await self._session.flush()

        conversation.last_message_preview_encrypted = preview_encrypted
        conversation.last_message_timestamp = message.created_at or datetime.now(UTC)
        if sender_is_customer:
            conversation.courier_unread_count += 1
        else:
            conversation.customer_unread_count += 1
        await self._session.flush()
        return message

    async def list_messages(
        self,
        conversation_id: uuid.UUID,
        *,
        limit: int,
        before_id: uuid.UUID | None,
    ) -> list[Message]:
        """Return a conversation's messages, newest first, keyset-paged."""
        query = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        if before_id is not None:
            anchor = await self._session.get(Message, before_id)
            if anchor is not None:
                query = query.where(
                    tuple_(Message.created_at, Message.id) < (anchor.created_at, anchor.id)
                )
        return list(await self._session.scalars(query))

    async def mark_read(self, conversation: Conversation, *, reader_is_customer: bool) -> None:
        """Reset the reader's unread counter and mark inbound messages read.

        Only is_read/read_at change on the messages — the append-only trigger permits it.
        """
        other_id = conversation.courier_id if reader_is_customer else conversation.customer_id
        if reader_is_customer:
            conversation.customer_unread_count = 0
        else:
            conversation.courier_unread_count = 0
        await self._session.execute(
            update(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.sender_id == other_id,
                Message.is_read.is_(False),
            )
            .values(is_read=True, read_at=datetime.now(UTC))
        )
        await self._session.flush()
