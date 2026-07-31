"""Chat routes: REST messaging plus a live WebSocket per conversation (SPEC SECTION 10).

Message content is encrypted at rest and decrypted only for participants. The WebSocket
is authenticated by an access token (``?token=``), verified against the same denylist as
the REST API, and only a conversation's two members may connect. Live delivery rides a
Redis pub/sub channel, so a message sent on any instance reaches every open socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Actor, get_db, get_redis, get_settings, require_role
from app.core.jwt import JwtError, decode_access_token
from app.core.ratelimit import RateLimiter
from app.models.enums import UserRole
from app.repositories.chat_repository import ChatRepository
from app.repositories.device_token_repository import DeviceTokenRepository
from app.schemas.chat import (
    InboxItemResponse,
    InboxResponse,
    MessagePage,
    MessageResponse,
    SendMessageRequest,
)
from app.services.chat_service import ChatMessage, ChatService, conversation_channel
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/api", tags=["chat"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
_Participant = require_role(UserRole.CUSTOMER, UserRole.COURIER)


def _service(request: Request, db: AsyncSession) -> ChatService:
    return ChatService(
        chat=ChatRepository(db), redis=get_redis(request), settings=get_settings(request)
    )


def _message(dto: ChatMessage) -> MessageResponse:
    return MessageResponse(
        id=dto.id,
        conversation_id=dto.conversation_id,
        sender_id=dto.sender_id,
        message_type=dto.message_type,
        content=dto.content,
        is_read=dto.is_read,
        created_at=dto.created_at,
    )


@router.get("/conversations", response_model=InboxResponse)
async def list_conversations(
    request: Request,
    db: DbDep,
    actor: Annotated[Actor, Depends(_Participant)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> InboxResponse:
    """List the caller's conversations, most-recent activity first (keyset paged)."""
    items = await _service(request, db).list_inbox(
        user_id=actor.id, limit=limit, before=_parse_cursor(cursor)
    )
    rows = [
        InboxItemResponse(
            conversation_id=i.conversation_id,
            order_id=i.order_id,
            other_user_id=i.other_user_id,
            last_message_preview=i.last_message_preview,
            unread_count=i.unread_count,
            last_message_timestamp=i.last_message_timestamp,
        )
        for i in items
    ]
    next_cursor = (
        f"{items[-1].last_message_timestamp}|{items[-1].conversation_id}"
        if len(items) == limit
        else None
    )
    return InboxResponse(items=rows, next_cursor=next_cursor)


@router.get("/conversations/{conversation_id}/messages", response_model=MessagePage)
async def list_messages(
    request: Request,
    db: DbDep,
    conversation_id: uuid.UUID,
    actor: Annotated[Actor, Depends(_Participant)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> MessagePage:
    """Return decrypted messages the caller can see (participant only)."""
    items = await _service(request, db).list_messages(
        conversation_id=conversation_id,
        actor_id=actor.id,
        limit=limit,
        before_id=uuid.UUID(cursor) if cursor else None,
    )
    next_cursor = items[-1].id if len(items) == limit else None
    return MessagePage(items=[_message(m) for m in items], next_cursor=next_cursor)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageResponse,
    status_code=201,
)
async def send_message(
    request: Request,
    db: DbDep,
    conversation_id: uuid.UUID,
    body: SendMessageRequest,
    actor: Annotated[Actor, Depends(_Participant)],
) -> MessageResponse:
    """Send a text message; participants receive it live over the WebSocket."""
    service = _service(request, db)
    dto = await service.send_message(
        conversation_id=conversation_id, sender_id=actor.id, text=body.text
    )
    # A live event must never race ahead of the durable row it announces.
    await db.commit()
    # Best-effort push to the recipient — the body carries NO message text (Restricted).
    conversation = await ChatRepository(db).get_for_actor(conversation_id, actor.id)
    if conversation is not None:
        recipient = (
            conversation.courier_id
            if actor.id == conversation.customer_id
            else conversation.customer_id
        )
        await NotificationService(
            devices=DeviceTokenRepository(db), push=request.app.state.clients.push
        ).notify_user(
            user_id=recipient,
            title="New message",
            body="You have a new message about your order.",
        )
    await service.publish_message(dto)
    return _message(dto)


@router.post("/conversations/{conversation_id}/read", status_code=204)
async def mark_read(
    request: Request,
    db: DbDep,
    conversation_id: uuid.UUID,
    actor: Annotated[Actor, Depends(_Participant)],
) -> None:
    """Mark the caller's inbound messages read and clear their unread count."""
    await _service(request, db).mark_read(conversation_id=conversation_id, actor_id=actor.id)


@router.websocket("/ws/conversations/{conversation_id}")
async def conversation_ws(websocket: WebSocket, conversation_id: uuid.UUID) -> None:
    """Live chat socket: pushes new messages and accepts sent messages.

    Authenticated by ``?token=`` (same verification as the REST API). Only the
    conversation's members may connect; anyone else is closed with policy violation.
    """
    actor = await _authenticate_ws(websocket)
    if actor is None:
        await websocket.close(code=4401)  # unauthenticated
        return

    factory = websocket.app.state.session_factory
    async with factory() as session:
        conversation = await ChatRepository(session).get_for_actor(conversation_id, actor.id)
    if conversation is None:
        await websocket.close(code=4403)  # not a participant
        return
    recipient_id = (
        conversation.courier_id
        if actor.id == conversation.customer_id
        else conversation.customer_id
    )

    await websocket.accept()
    redis: Redis = websocket.app.state.redis
    pubsub = redis.pubsub()
    await pubsub.subscribe(conversation_channel(conversation_id))
    reader = asyncio.create_task(_pump_pubsub_to_socket(pubsub, websocket))
    try:
        await _pump_socket_to_chat(websocket, conversation_id, actor, recipient_id, factory, redis)
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(conversation_channel(conversation_id))
            await pubsub.aclose()  # type: ignore[no-untyped-call]


async def _pump_pubsub_to_socket(pubsub: PubSub, websocket: WebSocket) -> None:
    async for event in pubsub.listen():
        if event.get("type") == "message":
            data = event["data"]
            await websocket.send_text(data.decode() if isinstance(data, bytes) else str(data))


async def _pump_socket_to_chat(
    websocket: WebSocket,
    conversation_id: uuid.UUID,
    actor: Actor,
    recipient_id: uuid.UUID,
    factory: object,
    redis: Redis,
) -> None:
    """Read inbound frames, guard them, persist the message, and push the recipient.

    Guards (audit SEC-4/LOG-2/LOG-3): frames over ``WS_MAX_FRAME_BYTES`` are dropped;
    each sender is throttled through the shared Redis rate limiter; a mid-connection
    ban closes the socket. Sends over the socket fire the same best-effort push the
    REST path fires (audit LOG-1), so the two paths never disagree.
    """
    settings = websocket.app.state.settings
    limiter = RateLimiter(
        redis,
        max_requests=settings.WS_RATE_LIMIT_MAX_MESSAGES,
        window_seconds=settings.WS_RATE_LIMIT_WINDOW_SECONDS,
    )
    while True:
        raw = await websocket.receive_text()
        if len(raw.encode("utf-8")) > settings.WS_MAX_FRAME_BYTES:
            continue  # oversized frame: dropped before any decrypt/persist work
        decision = await limiter.check_guarded(
            f"ws:{actor.id}", blocked_key=f"auth:banned:{actor.id}"
        )
        if decision.blocked:
            await websocket.close(code=4401)  # banned mid-connection
            return
        if not decision.allowed:
            continue  # over the per-user message ceiling: dropped
        text = _extract_text(raw)
        if not text:
            continue
        async with factory() as session:  # type: ignore[operator]
            service = ChatService(chat=ChatRepository(session), redis=redis, settings=settings)
            dto = await service.send_message(
                conversation_id=conversation_id, sender_id=actor.id, text=text
            )
            # Commit before any external side effect or live event can expose the row.
            await session.commit()
            # Same best-effort push as the REST path; NO message text in the body.
            await NotificationService(
                devices=DeviceTokenRepository(session), push=websocket.app.state.clients.push
            ).notify_user(
                user_id=recipient_id,
                title="New message",
                body="You have a new message about your order.",
            )
            await service.publish_message(dto)


def _extract_text(raw: str) -> str:
    """Extract the message text from a JSON frame; non-JSON frames are rejected.

    The WS contract matches REST (audit LOG-3): a frame must be a JSON object with a
    ``text`` field. Anything else returns "" and is dropped by the caller.
    """
    import json

    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if isinstance(parsed, dict):
        return str(parsed.get("text", "")).strip()
    return ""


async def _authenticate_ws(websocket: WebSocket) -> Actor | None:
    token = websocket.query_params.get("token", "")
    if not token:
        return None
    settings = websocket.app.state.settings
    try:
        claims = decode_access_token(settings, token)
    except JwtError:
        return None
    redis: Redis = websocket.app.state.redis
    if await redis.get(f"jwt:denylist:{claims.jti}"):
        return None
    try:
        role = UserRole(claims.role)
    except ValueError:
        return None
    if role not in (UserRole.CUSTOMER, UserRole.COURIER):
        return None
    return Actor(id=uuid.UUID(claims.sub), role=role, jti=claims.jti)


def _parse_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not cursor or "|" not in cursor:
        return None
    ts_raw, _, id_raw = cursor.partition("|")
    try:
        return datetime.fromisoformat(ts_raw), uuid.UUID(id_raw)
    except ValueError:
        return None
