"""StreamPay payment-link contract used by payment orchestration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class StreamPayCustomer:
    """The known payer attached to a StreamPay checkout."""

    external_id: str
    name: str
    phone_number: str
    email: str | None


@dataclass(frozen=True)
class StreamPayItem:
    """One immutable invoice item represented as a StreamPay one-time product."""

    name: str
    description: str | None
    amount: Decimal


@dataclass(frozen=True)
class StreamPayCheckout:
    """The hosted checkout URL and its provider payment-link ID."""

    payment_link_id: str
    payment_url: str


class StreamPayClient(ABC):
    """Creates hosted StreamPay checkouts and authenticates Stream webhooks."""

    @abstractmethod
    async def create_payment_link(
        self,
        *,
        reference: str,
        customer: StreamPayCustomer,
        items: tuple[StreamPayItem, ...],
        success_redirect_url: str | None,
        failure_redirect_url: str | None,
    ) -> StreamPayCheckout:
        """Create a one-time StreamPay payment link for the supplied invoice items."""

    @abstractmethod
    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verify StreamPay's timestamped HMAC signature in constant time."""
