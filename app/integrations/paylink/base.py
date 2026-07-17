"""The payment gateway contract (SPEC SECTION 5.1).

There are exactly two reasons to call the gateway — an invoice remainder and a
wallet top-up — both unified through ``payment_intents``. Services depend on this
ABC; only the Real/Fake implementations know Paylink's wire format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class GatewayCharge:
    """The gateway's response to a charge request."""

    transaction_no: str
    payment_url: str


class PaymentGateway(ABC):
    """Creates gateway charges and verifies webhook signatures."""

    @abstractmethod
    async def create_charge(
        self, *, amount: Decimal, order_number: str, callback_url: str
    ) -> GatewayCharge:
        """Create a charge for ``amount`` and return the transaction number and URL."""

    @abstractmethod
    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verify an HMAC signature over the RAW request body in constant time."""
