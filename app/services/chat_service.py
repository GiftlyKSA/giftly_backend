"""Chat service: encrypted messaging over an order's conversation (SPEC SECTION 10, 20).

Message content and the inbox preview are AES-256-GCM encrypted at rest, bound by AAD to
their row and column. Plaintext never lands in an unencrypted column (ADR 0004). On send
the service publishes the (decrypted) message to a Redis channel so every connected
WebSocket for that conversation — on any instance — receives it in real time.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.crypto import FieldCipher, build_aad, build_cipher
from app.core.exceptions import NotFoundError
from app.models import Conversation, Message
from app.repositories.chat_repository import ChatRepository

_PREVIEW_CHARS = 100


def conversation_channel(conversation_id: uuid.UUID) -> str:
    """The Redis pub/sub channel that carries a conversation's live messages."""
    return f"chat:conversation:{conversation_id}"


@dataclass(frozen=True)
class ChatMessage:
    """A decrypted message for the API/WS layer."""

    id: str
    conversation_id: str
    sender_id: str
    message_type: str
    content: str
    is_read: bool
    created_at: str


@dataclass(frozen=True)
class InboxItem:
    """A decrypted inbox row."""

    conversation_id: str
    order_id: str
    other_user_id: str
    last_message_preview: str | None
    unread_count: int
    last_message_timestamp: str


class ChatService:
    """Sends, lists, and marks-read encrypted chat messages."""

    def __init__(self, *, chat: ChatRepository, redis: Redis, settings: Settings) -> None:
        """Wire the chat repository, Redis (for WS fanout), and settings."""
        self._chat = chat
        self._redis = redis
        self._settings = settings

    def _cipher(self) -> FieldCipher:
        return build_cipher(
            self._settings.encryption_keys(), self._settings.FIELD_ENCRYPTION_KEY_VERSION
        )

    def _encrypt_content(self, conversation_id: uuid.UUID, text: str) -> str:
        aad = build_aad("messages", "content", str(conversation_id))
        return self._cipher().encrypt(text, aad)

    def _decrypt_content(self, conversation_id: uuid.UUID, blob: str) -> str:
        aad = build_aad("messages", "content", str(conversation_id))
        return self._cipher().decrypt(blob, aad)

    def _encrypt_preview(self, conversation_id: uuid.UUID, text: str) -> str:
        aad = build_aad("conversations", "last_message_preview", str(conversation_id))
        return self._cipher().encrypt(text[:_PREVIEW_CHARS], aad)

    def _decrypt_preview(self, conversation_id: uuid.UUID, blob: str) -> str:
        aad = build_aad("conversations", "last_message_preview", str(conversation_id))
        return self._cipher().decrypt(blob, aad)

    async def _require_conversation(
        self, conversation_id: uuid.UUID, actor_id: uuid.UUID
    ) -> Conversation:
        conversation = await self._chat.get_for_actor(conversation_id, actor_id)
        if conversation is None:
            raise NotFoundError("Conversation not found.")
        return conversation

    async def send_message(
        self, *, conversation_id: uuid.UUID, sender_id: uuid.UUID, text: str
    ) -> ChatMessage:
        """Encrypt and append a message, then publish it for live delivery.

        Raises:
            NotFoundError: The sender does not participate in the conversation.
        """
        conversation = await self._require_conversation(conversation_id, sender_id)
        content = self._encrypt_content(conversation_id, text)
        preview = self._encrypt_preview(conversation_id, text)
        message = await self._chat.add_message(
            conversation=conversation,
            sender_id=sender_id,
            content_encrypted=content,
            preview_encrypted=preview,
            sender_is_customer=sender_id == conversation.customer_id,
        )
        dto = self._to_dto(message, text)
        await self._publish(conversation_id, dto)
        return dto

    async def list_messages(
        self,
        *,
        conversation_id: uuid.UUID,
        actor_id: uuid.UUID,
        limit: int,
        before_id: uuid.UUID | None,
    ) -> list[ChatMessage]:
        """Return decrypted messages for a participant, newest first."""
        await self._require_conversation(conversation_id, actor_id)
        rows = await self._chat.list_messages(conversation_id, limit=limit, before_id=before_id)
        return [
            self._to_dto(m, self._decrypt_content(conversation_id, m.content_encrypted))
            for m in rows
        ]

    async def mark_read(self, *, conversation_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        """Mark a participant's inbound messages read and clear their unread count."""
        conversation = await self._require_conversation(conversation_id, actor_id)
        await self._chat.mark_read(
            conversation, reader_is_customer=actor_id == conversation.customer_id
        )

    async def list_inbox(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        before: tuple[datetime, uuid.UUID] | None,
    ) -> list[InboxItem]:
        """Return a user's conversations with decrypted previews and unread counts."""
        conversations = await self._chat.list_for_user(user_id, limit=limit, before=before)
        items: list[InboxItem] = []
        for conv in conversations:
            is_customer = conv.customer_id == user_id
            preview = None
            if conv.last_message_preview_encrypted is not None:
                preview = self._decrypt_preview(conv.id, conv.last_message_preview_encrypted)
            items.append(
                InboxItem(
                    conversation_id=str(conv.id),
                    order_id=str(conv.order_id),
                    other_user_id=str(conv.courier_id if is_customer else conv.customer_id),
                    last_message_preview=preview,
                    unread_count=(
                        conv.customer_unread_count if is_customer else conv.courier_unread_count
                    ),
                    last_message_timestamp=conv.last_message_timestamp.isoformat(),
                )
            )
        return items

    async def _publish(self, conversation_id: uuid.UUID, message: ChatMessage) -> None:
        payload = json.dumps(
            {
                "id": message.id,
                "conversation_id": message.conversation_id,
                "sender_id": message.sender_id,
                "message_type": message.message_type,
                "content": message.content,
                "is_read": message.is_read,
                "created_at": message.created_at,
            }
        )
        await self._redis.publish(conversation_channel(conversation_id), payload)

    @staticmethod
    def _to_dto(message: Message, content: str) -> ChatMessage:
        return ChatMessage(
            id=str(message.id),
            conversation_id=str(message.conversation_id),
            sender_id=str(message.sender_id),
            message_type=str(message.message_type),
            content=content,
            is_read=message.is_read,
            created_at=message.created_at.isoformat(),
        )
