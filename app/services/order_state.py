"""The order state machine (SPEC SECTION 9).

The ONLY legal transitions live here, in one transition table — never scattered
if-statements. Any transition not in the table raises InvalidStateTransitionError.
"""

from __future__ import annotations

from app.core.exceptions import InvalidStateTransitionError
from app.models.enums import OrderStatus

# The complete, authoritative set of legal order transitions.
_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.NEW: {OrderStatus.ASSIGNED, OrderStatus.CANCELLED},
    OrderStatus.ASSIGNED: {OrderStatus.WAITING_PAYMENT, OrderStatus.CANCELLED},
    OrderStatus.WAITING_PAYMENT: {
        OrderStatus.IN_PROGRESS,
        OrderStatus.ASSIGNED,  # invoice expired/cancelled
        OrderStatus.CANCELLED,
    },
    OrderStatus.IN_PROGRESS: {OrderStatus.DELIVERED, OrderStatus.DISPUTED},
    OrderStatus.DELIVERED: {OrderStatus.COMPLETED, OrderStatus.DISPUTED},
    OrderStatus.DISPUTED: {OrderStatus.COMPLETED, OrderStatus.REFUNDED},
    OrderStatus.COMPLETED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REFUNDED: set(),
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    """Return whether ``current -> target`` is a legal order transition."""
    return target in _TRANSITIONS.get(current, set())


def assert_transition(current: OrderStatus, target: OrderStatus) -> None:
    """Raise unless ``current -> target`` is legal.

    Raises:
        InvalidStateTransitionError: The transition is not in the table.
    """
    if not can_transition(current, target):
        raise InvalidStateTransitionError(
            f"Cannot move an order from {current.value} to {target.value}."
        )
