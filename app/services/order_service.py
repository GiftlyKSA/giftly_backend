"""Order lifecycle service (SPEC SECTION 20.C, 9).

Owns order creation (with media validation and the concurrency limits), the accept
race (a Redis lock AND a SELECT ... FOR UPDATE — both layers deliberate), cancellation
through the single state machine, and the radar/list reads. The actor id always comes
from the verified JWT, never the request body.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import build_aad, build_cipher
from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    OrderAlreadyAssignedError,
    ValidationDomainError,
)
from app.core.locks import LockNotAcquiredError, redis_lock
from app.models import Order
from app.models.enums import MediaType, MessageType, OrderStatus, UserRole, UserStatus
from app.repositories.courier_repository import CourierRepository
from app.repositories.message_repository import MessageWriter
from app.repositories.order_repository import OrderRepository
from app.repositories.user_repository import UserRepository
from app.services.media_service import MediaService
from app.services.order_state import assert_transition

# Saudi Arabia bounding box (approx) — reject coordinates outside it early.
_SA_LAT = (16.0, 33.0)
_SA_LNG = (34.0, 56.0)
_MAX_CUSTOMER_ACTIVE = 5
_MAX_COURIER_ACTIVE = 3
_MAX_REQUEST_MEDIA = 3
_ACCEPT_LOCK_TTL = 10


@dataclass(frozen=True)
class NewOrderInput:
    """Validated inputs for creating an order."""

    description: str | None
    delivery_city: str
    latitude: float
    longitude: float
    delivery_date: date
    request_media_keys: list[str]


class OrderService:
    """Creates, accepts, cancels, and lists orders."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        orders: OrderRepository,
        users: UserRepository,
        couriers: CourierRepository,
        media: MediaService,
        messages: MessageWriter,
        redis: Redis,
        settings: Settings,
    ) -> None:
        """Wire the collaborators the order flows need."""
        self._session = session
        self._orders = orders
        self._users = users
        self._couriers = couriers
        self._media = media
        self._messages = messages
        self._redis = redis
        self._settings = settings

    async def create_order(self, *, customer_id: uuid.UUID, data: NewOrderInput) -> Order:
        """Create a NEW order after validating limits, coordinates, and media.

        Raises:
            ValidationDomainError: Coordinates out of range, too many media keys, or a
                media object that fails validation.
            ConflictError: The customer already has the maximum active orders.
        """
        if not (_SA_LAT[0] <= data.latitude <= _SA_LAT[1]):
            raise ValidationDomainError("Latitude is outside the service area.")
        if not (_SA_LNG[0] <= data.longitude <= _SA_LNG[1]):
            raise ValidationDomainError("Longitude is outside the service area.")
        if len(data.request_media_keys) > _MAX_REQUEST_MEDIA:
            raise ValidationDomainError("At most 3 request photos are allowed.")
        if await self._orders.count_customer_active(customer_id) >= _MAX_CUSTOMER_ACTIVE:
            raise ConflictError("You have reached the maximum number of active orders.")

        # Validate every media object before writing anything (HEAD + magic bytes).
        for key in data.request_media_keys:
            await self._media.confirm(key)

        order = await self._orders.create(
            customer_id=customer_id,
            description=data.description,
            delivery_city=data.delivery_city,
            longitude=data.longitude,
            latitude=data.latitude,
            delivery_date=data.delivery_date,
            address_note=None,
        )
        for key in data.request_media_keys:
            await self._orders.add_media(
                order_id=order.id,
                uploaded_by_user_id=customer_id,
                media_type=MediaType.CUSTOMER_REQUEST,
                storage_key=key,
                content_type="image/jpeg",
                byte_size=0,
            )
        return order

    async def accept_order(self, *, order_id: uuid.UUID, courier_id: uuid.UUID) -> Order:
        """Accept a NEW order as a verified courier (Redis lock + FOR UPDATE).

        Raises:
            NotFoundError: The order does not exist.
            ForbiddenError: The courier is not verified/active, or over the assignment
                limit.
            OrderAlreadyAssignedError: Another courier won the race, or the order is no
                longer NEW.
        """
        await self._require_active_verified_courier(courier_id)
        if await self._orders.count_courier_active(courier_id) >= _MAX_COURIER_ACTIVE:
            raise ForbiddenError("You have reached the maximum number of active assignments.")

        lock_key = f"lock:order_accept:{order_id}"
        try:
            async with redis_lock(self._redis, lock_key, ttl_seconds=_ACCEPT_LOCK_TTL):
                return await self._assign_locked(order_id, courier_id)
        except LockNotAcquiredError as exc:
            # Someone else holds the accept lock right now.
            raise OrderAlreadyAssignedError() from exc

    async def _assign_locked(self, order_id: uuid.UUID, courier_id: uuid.UUID) -> Order:
        # The DB row lock is belt-and-braces: if Redis ever fails open, the DB still
        # serializes the assignment.
        order = await self._orders.lock(order_id)
        if order is None:
            raise NotFoundError("Order not found.")
        if order.status is not OrderStatus.NEW:
            raise OrderAlreadyAssignedError()

        assert_transition(order.status, OrderStatus.ASSIGNED)
        order.courier_id = courier_id
        order.status = OrderStatus.ASSIGNED
        order.assigned_at = self._orders.now()
        conversation = await self._orders.create_conversation(
            order_id=order.id, customer_id=order.customer_id, courier_id=courier_id
        )
        await self._write_system_message(
            conversation.id, courier_id, "A courier accepted your order."
        )
        await self._orders.flush()
        return order

    async def cancel_order(
        self, *, order_id: uuid.UUID, actor_id: uuid.UUID, reason: str | None
    ) -> Order:
        """Cancel an order before it is in progress (either party).

        Raises:
            NotFoundError: No such order for this actor.
            InvalidStateTransitionError: The order is past the cancellable window.
        """
        order = await self._orders.get_for_actor(order_id, actor_id)
        if order is None:
            raise NotFoundError("Order not found.")
        assert_transition(order.status, OrderStatus.CANCELLED)
        order.status = OrderStatus.CANCELLED
        order.cancelled_reason = reason
        await self._orders.flush()
        return order

    async def get_order_for_actor(self, *, order_id: uuid.UUID, actor_id: uuid.UUID) -> Order:
        """Return an order the actor participates in, else 404 (no existence leak)."""
        order = await self._orders.get_for_actor(order_id, actor_id)
        if order is None:
            raise NotFoundError("Order not found.")
        return order

    async def _require_active_verified_courier(self, courier_id: uuid.UUID) -> None:
        user = await self._users.get(courier_id)
        if (
            user is None
            or user.role is not UserRole.COURIER
            or user.status is not UserStatus.ACTIVE
        ):
            raise ForbiddenError("Only an active courier may accept orders.")
        profile = await self._couriers.get(courier_id)
        if profile is None or not profile.is_verified:
            raise ForbiddenError("Your courier account is not verified yet.")

    async def _write_system_message(
        self, conversation_id: uuid.UUID, sender_id: uuid.UUID, text: str
    ) -> None:
        cipher = build_cipher(
            self._settings.encryption_keys(), self._settings.FIELD_ENCRYPTION_KEY_VERSION
        )
        content = cipher.encrypt(text, build_aad("messages", "content", str(conversation_id)))
        await self._messages.add_system_message(
            conversation_id=conversation_id,
            sender_id=sender_id,
            content_encrypted=content,
            message_type=MessageType.SYSTEM,
        )
