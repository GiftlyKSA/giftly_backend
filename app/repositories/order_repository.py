"""Order and order-media persistence (SPEC SECTION 10, 13, 20.C).

Spatial writes put longitude FIRST in ``ST_MakePoint`` — reversing it puts Jeddah in
Antarctica and every geofence check silently fails. Money-free ownership is enforced
in the query (customer or courier), never fetch-then-compare.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Order, OrderMedia
from app.models.enums import MediaType, OrderStatus

# Statuses that count against a customer's concurrent-order limit.
_CUSTOMER_ACTIVE = (
    OrderStatus.NEW,
    OrderStatus.ASSIGNED,
    OrderStatus.WAITING_PAYMENT,
    OrderStatus.IN_PROGRESS,
    OrderStatus.DELIVERED,
    OrderStatus.DISPUTED,
)
# Statuses that count against a courier's concurrent-assignment limit.
_COURIER_ACTIVE = (
    OrderStatus.ASSIGNED,
    OrderStatus.WAITING_PAYMENT,
    OrderStatus.IN_PROGRESS,
)


class OrderRepository:
    """Creates and reads orders, with FOR UPDATE locking for the accept race."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def create(
        self,
        *,
        customer_id: uuid.UUID,
        description: str | None,
        delivery_city: str,
        longitude: float,
        latitude: float,
        delivery_date: date,
        address_note: str | None,
    ) -> Order:
        """Insert a NEW order; the point is built lng-first (ST_MakePoint(x, y))."""
        order = Order(
            customer_id=customer_id,
            description=description,
            delivery_city=delivery_city,
            delivery_location=func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326),
            delivery_date=delivery_date,
            delivery_address_note=address_note,
            status=OrderStatus.NEW,
        )
        self._session.add(order)
        await self._session.flush()
        return order

    async def add_media(
        self,
        *,
        order_id: uuid.UUID,
        uploaded_by_user_id: uuid.UUID,
        media_type: MediaType,
        storage_key: str,
        content_type: str,
        byte_size: int,
    ) -> None:
        """Attach a media object (customer request or delivery proof) to an order."""
        self._session.add(
            OrderMedia(
                order_id=order_id,
                uploaded_by_user_id=uploaded_by_user_id,
                media_type=media_type,
                storage_key=storage_key,
                content_type=content_type,
                byte_size=byte_size,
            )
        )
        await self._session.flush()

    async def get(self, order_id: uuid.UUID) -> Order | None:
        """Return an order by id, or None."""
        return await self._session.get(Order, order_id)

    async def get_for_actor(self, order_id: uuid.UUID, actor_id: uuid.UUID) -> Order | None:
        """Return an order only if the actor is its customer or courier (ownership)."""
        result: Order | None = await self._session.scalar(
            select(Order).where(
                Order.id == order_id,
                (Order.customer_id == actor_id) | (Order.courier_id == actor_id),
            )
        )
        return result

    async def lock(self, order_id: uuid.UUID) -> Order | None:
        """Load an order FOR UPDATE (the DB layer of the accept race)."""
        result: Order | None = await self._session.scalar(
            select(Order).where(Order.id == order_id).with_for_update()
        )
        return result

    async def count_customer_active(self, customer_id: uuid.UUID) -> int:
        """Count a customer's non-terminal orders (concurrency limit)."""
        total = await self._session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.customer_id == customer_id, Order.status.in_(_CUSTOMER_ACTIVE))
        )
        return int(total or 0)

    async def count_courier_active(self, courier_id: uuid.UUID) -> int:
        """Count a courier's active assignments (concurrency limit)."""
        total = await self._session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.courier_id == courier_id, Order.status.in_(_COURIER_ACTIVE))
        )
        return int(total or 0)

    async def list_for_customer(
        self,
        customer_id: uuid.UUID,
        *,
        status: OrderStatus | None,
        limit: int,
        before_id: uuid.UUID | None,
    ) -> list[Order]:
        """Return a customer's orders, newest first, keyset-paged."""
        query = select(Order).where(Order.customer_id == customer_id)
        if status is not None:
            query = query.where(Order.status == status)
        return await self._page(query, limit, before_id)

    async def list_available(
        self, city: str, *, limit: int, before_id: uuid.UUID | None
    ) -> list[Order]:
        """Return NEW orders in a city (the courier radar), newest first, keyset-paged."""
        query = select(Order).where(Order.delivery_city == city, Order.status == OrderStatus.NEW)
        return await self._page(query, limit, before_id)

    async def _page(
        self, query: Select[tuple[Order]], limit: int, before_id: uuid.UUID | None
    ) -> list[Order]:
        query = query.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)
        if before_id is not None:
            anchor = await self._session.get(Order, before_id)
            if anchor is not None:
                query = query.where(
                    tuple_(Order.created_at, Order.id) < (anchor.created_at, anchor.id)
                )
        return list(await self._session.scalars(query))

    async def create_conversation(
        self, *, order_id: uuid.UUID, customer_id: uuid.UUID, courier_id: uuid.UUID
    ) -> Conversation:
        """Create the order's conversation (unique per order) on assignment."""
        conversation = Conversation(
            order_id=order_id, customer_id=customer_id, courier_id=courier_id
        )
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def coords(self, order_id: uuid.UUID) -> tuple[float, float] | None:
        """Return an order's (longitude, latitude), extracted from the geometry."""
        row = (
            await self._session.execute(
                select(
                    func.ST_X(Order.delivery_location), func.ST_Y(Order.delivery_location)
                ).where(Order.id == order_id)
            )
        ).first()
        return (float(row[0]), float(row[1])) if row is not None else None

    async def flush(self) -> None:
        """Flush pending writes."""
        await self._session.flush()

    @staticmethod
    def now() -> datetime:
        """Return the current UTC time (single source for assigned/cancelled stamps)."""
        from datetime import UTC

        return datetime.now(UTC)
