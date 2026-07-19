"""Key-rotation tests: mutable Restricted columns re-encrypt; messages stay put."""

from __future__ import annotations

import base64
import os
import uuid
from datetime import date

from app.core.config import Settings
from app.core.crypto import blob_version, build_aad, build_cipher
from app.models import Conversation, CourierProfile, Order, User, Wallet, Withdrawal
from app.models.enums import OrderStatus, UserRole, WalletType, WithdrawalStatus
from app.services.key_rotation_service import KeyRotationService
from geoalchemy2 import WKTElement
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_test_settings

_KEY_V1 = base64.b64encode(b"\x00" * 32).decode()
_KEY_V2 = base64.b64encode(b"\x11" * 32).decode()


def _settings_two_keys() -> Settings:
    """Settings with two key versions, active = 2."""
    overrides: dict[str, object] = {
        "FIELD_ENCRYPTION_KEYS": f'{{"1":"{_KEY_V1}","2":"{_KEY_V2}"}}',
        "FIELD_ENCRYPTION_KEY_VERSION": 2,
    }
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    return make_test_settings(**overrides)


async def test_rotation_reencrypts_mutable_columns(db_session: AsyncSession) -> None:
    settings = _settings_two_keys()
    keys = settings.encryption_keys()
    v1_cipher = build_cipher(keys, 1)  # write everything under the OLD version

    courier = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.COURIER)
    db_session.add(courier)
    await db_session.flush()
    profile = CourierProfile(
        user_id=courier.id,
        city_of_residence="Jeddah",
        is_verified=True,
        national_id_encrypted=v1_cipher.encrypt(
            "1234567890", build_aad("courier_profiles", "national_id", str(courier.id))
        ),
    )
    db_session.add(profile)

    customer = User(phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}", role=UserRole.CUSTOMER)
    db_session.add(customer)
    await db_session.flush()
    order = Order(
        customer_id=customer.id,
        courier_id=courier.id,
        delivery_city="Jeddah",
        delivery_location=WKTElement("POINT(39.2 21.5)", srid=4326),
        delivery_date=date.today(),
        status=OrderStatus.IN_PROGRESS,
    )
    db_session.add(order)
    await db_session.flush()
    conv = Conversation(order_id=order.id, customer_id=customer.id, courier_id=courier.id)
    conv.last_message_preview_encrypted = v1_cipher.encrypt(
        "see you soon", build_aad("conversations", "last_message_preview", "PLACEHOLDER")
    )
    db_session.add(conv)
    await db_session.flush()
    # Fix the preview AAD now that we know the conversation id.
    conv.last_message_preview_encrypted = v1_cipher.encrypt(
        "see you soon", build_aad("conversations", "last_message_preview", str(conv.id))
    )

    wallet = Wallet(user_id=courier.id, type=WalletType.COURIER)
    db_session.add(wallet)
    await db_session.flush()
    withdrawal = Withdrawal(
        courier_id=courier.id,
        wallet_id=wallet.id,
        amount=100,
        iban_encrypted="PLACEHOLDER",
        iban_last4="6789",
        status=WithdrawalStatus.REQUESTED,
    )
    db_session.add(withdrawal)
    await db_session.flush()
    withdrawal.iban_encrypted = v1_cipher.encrypt(
        "SA0380000000608010167519", build_aad("withdrawals", "iban", str(withdrawal.id))
    )
    await db_session.flush()

    # Everything is under version 1 before rotation.
    assert blob_version(profile.national_id_encrypted) == 1
    assert blob_version(conv.last_message_preview_encrypted) == 1
    assert blob_version(withdrawal.iban_encrypted) == 1

    # Rotation is global; the shared test DB has other committed rows too, so assert our
    # rows were counted (>= 1) rather than an exact total.
    report = await KeyRotationService(session=db_session, settings=settings).rotate()
    assert report.courier_national_id >= 1
    assert report.conversation_preview >= 1
    assert report.withdrawal_iban >= 1

    # Now under version 2, and still decrypt to the same plaintext under the active cipher.
    active = build_cipher(keys, 2)
    assert blob_version(profile.national_id_encrypted) == 2
    assert (
        active.decrypt(
            profile.national_id_encrypted,
            build_aad("courier_profiles", "national_id", str(courier.id)),
        )
        == "1234567890"
    )
    assert blob_version(withdrawal.iban_encrypted) == 2
    assert (
        active.decrypt(
            withdrawal.iban_encrypted, build_aad("withdrawals", "iban", str(withdrawal.id))
        )
        == "SA0380000000608010167519"
    )

    # Re-running is a no-op (nothing left under an old version).
    again = await KeyRotationService(session=db_session, settings=settings).rotate()
    assert again.total == 0
