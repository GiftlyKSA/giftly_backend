"""Deterministic in-memory Paylink double (SPEC SECTION 5.1 / 23).

Returns deterministic transaction numbers and a local simulate URL so a developer
runs the whole payment flow with zero Paylink credentials. The dev simulate route
fires a correctly-signed webhook at our own REAL handler — the fake never bypasses
the webhook handler, only the external gateway.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import secrets
from decimal import Decimal

from app.core.config import Environment
from app.integrations._guard import forbid_in_production
from app.integrations.paylink.base import GatewayCharge, PaymentGateway

_DEFAULT_WEBHOOK_KEY = hashlib.sha256(b"safe-gift-fake-paylink-key").hexdigest()


class FakePaylinkClient(PaymentGateway):
    """A gateway that records charges and signs webhooks with a test secret."""

    def __init__(
        self,
        environment: Environment,
        webhook_secret: str | None = None,
    ) -> None:
        """Refuse construction in production and seed a per-instance unique counter."""
        forbid_in_production(environment, type(self).__name__)
        self._counter = itertools.count(1)
        # A random per-instance prefix keeps transaction numbers globally unique across
        # app instances (real gateway txns are unique; the DB has a unique index on them).
        self._prefix = secrets.token_hex(4)
        self._webhook_secret = (webhook_secret or _DEFAULT_WEBHOOK_KEY).encode("utf-8")
        self.charges: dict[str, Decimal] = {}

    async def create_charge(
        self, *, amount: Decimal, order_number: str, callback_url: str
    ) -> GatewayCharge:
        """Return a unique transaction number and a local simulate URL."""
        txn = f"FAKE-TXN-{self._prefix}-{next(self._counter):08d}"
        self.charges[txn] = amount
        return GatewayCharge(
            transaction_no=txn,
            payment_url=f"http://localhost:8000/api/dev/paylink/simulate?transaction_no={txn}",
        )

    def sign(self, raw_body: bytes) -> str:
        """Sign a webhook body with the test secret (used by the simulate route)."""
        return hmac.new(self._webhook_secret, raw_body, hashlib.sha256).hexdigest()

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verify a signature in constant time against the test secret."""
        return hmac.compare_digest(self.sign(raw_body), signature)
