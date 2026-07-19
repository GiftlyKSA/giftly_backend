"""Pydantic contracts for the chat endpoints (SPEC SECTION 10, 20)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class SendMessageRequest(BaseModel):
    """Send a text message into a conversation."""

    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class MessageResponse(BaseModel):
    """A decrypted chat message."""

    id: str
    conversation_id: str
    sender_id: str
    message_type: str
    content: str
    is_read: bool
    created_at: str


class MessagePage(BaseModel):
    """A keyset page of messages (newest first)."""

    items: list[MessageResponse]
    next_cursor: str | None = None


class InboxItemResponse(BaseModel):
    """A conversation row for the inbox, with a decrypted preview."""

    conversation_id: str
    order_id: str
    other_user_id: str
    last_message_preview: str | None
    unread_count: int
    last_message_timestamp: str


class InboxResponse(BaseModel):
    """A keyset page of conversations."""

    items: list[InboxItemResponse]
    next_cursor: str | None = Field(
        None, description="Opaque `<iso8601>|<uuid>` cursor for the next page."
    )
