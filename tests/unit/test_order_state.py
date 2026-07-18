"""Tests for the order state machine (SPEC SECTION 9)."""

from __future__ import annotations

import pytest
from app.core.exceptions import InvalidStateTransitionError
from app.models.enums import OrderStatus
from app.services.order_state import assert_transition, can_transition


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.NEW, OrderStatus.ASSIGNED),
        (OrderStatus.NEW, OrderStatus.CANCELLED),
        (OrderStatus.ASSIGNED, OrderStatus.WAITING_PAYMENT),
        (OrderStatus.WAITING_PAYMENT, OrderStatus.IN_PROGRESS),
        (OrderStatus.WAITING_PAYMENT, OrderStatus.ASSIGNED),
        (OrderStatus.IN_PROGRESS, OrderStatus.DELIVERED),
        (OrderStatus.IN_PROGRESS, OrderStatus.DISPUTED),
        (OrderStatus.DELIVERED, OrderStatus.COMPLETED),
        (OrderStatus.DISPUTED, OrderStatus.REFUNDED),
    ],
)
def test_legal_transitions(current: OrderStatus, target: OrderStatus) -> None:
    assert can_transition(current, target)
    assert_transition(current, target)  # does not raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.NEW, OrderStatus.IN_PROGRESS),
        (OrderStatus.NEW, OrderStatus.COMPLETED),
        (OrderStatus.IN_PROGRESS, OrderStatus.CANCELLED),
        (OrderStatus.DELIVERED, OrderStatus.CANCELLED),
        (OrderStatus.COMPLETED, OrderStatus.DELIVERED),
        (OrderStatus.CANCELLED, OrderStatus.NEW),
        (OrderStatus.REFUNDED, OrderStatus.COMPLETED),
    ],
)
def test_illegal_transitions_raise(current: OrderStatus, target: OrderStatus) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidStateTransitionError):
        assert_transition(current, target)
