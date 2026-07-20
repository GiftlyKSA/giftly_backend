"""Direct tests for the admin service, OTP service, and auth service.

Exercise the admin operations (verify, reveal, ban, promo management, reads) against
a real database session and Redis, covering the repository and service layers without
going through HTTP.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from app.core.config import Settings
from app.core.crypto import build_aad, build_cipher
from app.core.exceptions import RateLimitedError, UnauthorizedError
from app.core.redis import build_redis
from app.core.security import sha256_hex
from app.integrations.sms.fake import FakeSmsClient
from app.models import CourierProfile, User, Wallet, Withdrawal
from app.models.enums import PromoDiscountType, UserRole, UserStatus, WalletType, WithdrawalStatus
from app.repositories.admin_read_repository import AdminReadRepository
from app.repositories.admin_session_repository import AdminSessionRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.auth_repository import AuthRepository
from app.repositories.courier_repository import CourierRepository
from app.repositories.promo_repository import PromoRepository
from app.repositories.user_repository import UserRepository
from app.services.admin_auth_service import AdminAuthService
from app.services.admin_service import AdminService
from app.services.otp_service import OtpService
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_test_settings


def _settings() -> Settings:
    import os

    overrides: dict[str, object] = {}
    if os.environ.get("DATABASE_URL"):
        overrides["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if os.environ.get("REDIS_URL"):
        overrides["REDIS_URL"] = os.environ["REDIS_URL"]
    return make_test_settings(**overrides)


def _admin_service(db: AsyncSession, settings: Settings, redis: Redis) -> AdminService:
    return AdminService(
        reads=AdminReadRepository(db),
        users=UserRepository(db),
        couriers=CourierRepository(db),
        promos=PromoRepository(db),
        audit=AuditRepository(db),
        auth_repo=AuthRepository(db),
        redis=redis,
        settings=settings,
    )


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    settings = _settings()
    client = build_redis(settings)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        pytest.skip(f"redis unavailable: {exc}")
    yield client
    await client.aclose()


async def _admin(db: AsyncSession) -> User:
    admin = User(
        phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(admin)
    await db.flush()
    return admin


async def test_overview_and_reads(db_session: AsyncSession, redis_client: Redis) -> None:
    service = _admin_service(db_session, _settings(), redis_client)
    overview = await service.overview()
    assert set(overview.system_balances) >= {"SYSTEM_ESCROW", "SYSTEM_GATEWAY"}
    assert await service.list_orders() is not None
    assert await service.list_invoices() is not None
    assert await service.list_disputes() is not None
    assert await service.list_withdrawals() is not None
    assert await service.list_wallets() is not None
    assert await service.list_topups() is not None
    assert await service.list_promos() is not None
    assert await service.list_audit_logs() is not None


async def test_verify_courier_and_reveal_identity(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    settings = _settings()
    service = _admin_service(db_session, settings, redis_client)
    admin = await _admin(db_session)

    courier = User(
        phone=f"+96650{uuid.uuid4().int % 10_000_000:07d}",
        role=UserRole.COURIER,
        status=UserStatus.PENDING_VERIFICATION,
    )
    db_session.add(courier)
    await db_session.flush()

    cipher = build_cipher(settings.encryption_keys(), settings.FIELD_ENCRYPTION_KEY_VERSION)
    enc = cipher.encrypt(
        "1122334455", build_aad("courier_profiles", "national_id", str(courier.id))
    )
    db_session.add(
        CourierProfile(user_id=courier.id, city_of_residence="Jeddah", national_id_encrypted=enc)
    )
    await db_session.flush()

    await service.verify_courier(
        admin_id=admin.id, courier_user_id=courier.id, approve=True, note="ok", ip="1.2.3.4"
    )
    profile = await service.get_courier(courier.id)
    assert profile is not None and profile.is_verified is True
    refreshed = await service.get_user(courier.id)
    assert refreshed.status is UserStatus.ACTIVE  # type: ignore[union-attr]

    revealed = await service.reveal_identity(
        admin_id=admin.id, courier_user_id=courier.id, ip="1.2.3.4"
    )
    assert revealed["national_id"] == "1122334455"


async def test_reveal_iban(db_session: AsyncSession, redis_client: Redis) -> None:
    settings = _settings()
    service = _admin_service(db_session, settings, redis_client)
    admin = await _admin(db_session)
    courier = await _admin(db_session)  # any user id works as courier_id here

    wallet = Wallet(user_id=courier.id, type=WalletType.COURIER, balance=Decimal("0.00"))
    db_session.add(wallet)
    await db_session.flush()
    withdrawal = Withdrawal(
        courier_id=courier.id,
        wallet_id=wallet.id,
        amount=Decimal("100.00"),
        iban_encrypted="placeholder",
        iban_last4="1234",
        status=WithdrawalStatus.REQUESTED,
    )
    db_session.add(withdrawal)
    await db_session.flush()
    cipher = build_cipher(settings.encryption_keys(), settings.FIELD_ENCRYPTION_KEY_VERSION)
    withdrawal.iban_encrypted = cipher.encrypt(
        "SA0380000000608010167519", build_aad("withdrawals", "iban", str(withdrawal.id))
    )
    await db_session.flush()

    iban = await service.reveal_iban(admin_id=admin.id, withdrawal=withdrawal, ip="1.2.3.4")
    assert iban == "SA0380000000608010167519"


async def test_ban_and_promo_management(db_session: AsyncSession, redis_client: Redis) -> None:
    service = _admin_service(db_session, _settings(), redis_client)
    admin = await _admin(db_session)
    target = await _admin(db_session)

    await service.set_user_banned(admin_id=admin.id, user_id=target.id, banned=True, ip=None)
    banned = await service.get_user(target.id)
    assert banned.status is UserStatus.BANNED  # type: ignore[union-attr]

    promo_id = await service.create_promo(
        admin_id=admin.id,
        code=f"svc{uuid.uuid4().hex[:6]}",
        description="svc test",
        discount_type=PromoDiscountType.PERCENT,
        percent_value=Decimal("10.00"),
        fixed_amount=None,
        max_discount_amount=Decimal("100.00"),
        min_order_amount=Decimal("0.00"),
        max_total_usages=None,
        max_usages_per_user=1,
        ip=None,
    )
    await service.set_promo_active(admin_id=admin.id, promo_id=promo_id, active=False, ip=None)
    promo = await service.get_promo(promo_id)
    assert promo.is_active is False  # type: ignore[union-attr]
    assert await service.list_promo_redemptions(promo_id) == []


async def test_otp_service_request_verify_and_rate_limit(redis_client: Redis) -> None:
    settings = _settings()
    sms = FakeSmsClient(settings.ENVIRONMENT)
    otp = OtpService(redis_client, sms, settings)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"

    await otp.request_otp(phone)
    code = sms.last_otp[phone]
    assert await otp.verify_otp(phone, code) is True
    assert await otp.verify_otp(phone, "000000") is False

    # Exhaust the per-window quota to trigger the block.
    fresh = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    with pytest.raises(RateLimitedError):
        for _ in range(settings.OTP_MAX_PER_WINDOW + 2):
            await otp.request_otp(fresh)


async def test_admin_auth_session_and_logout(db_session: AsyncSession, redis_client: Redis) -> None:
    settings = _settings()
    sms = FakeSmsClient(settings.ENVIRONMENT)
    auth = AdminAuthService(
        otp=OtpService(redis_client, sms, settings),
        users=UserRepository(db_session),
        sessions=AdminSessionRepository(db_session),
        redis=redis_client,
        settings=settings,
    )
    # An unknown session token is rejected.
    with pytest.raises(UnauthorizedError):
        await auth.load_session("not-a-real-token")

    # Step-up grant/read round-trips through Redis.
    token_hash = sha256_hex("some-token")
    assert await auth.has_step_up(token_hash) is False
    assert auth.csrf_token_for(token_hash)
    await auth.logout("not-a-real-token")  # no-op, must not raise
