"""Direct tests for the admin service, OTP service, and auth service.

Exercise the admin operations (verify, reveal, ban, promo management, reads) against
a real database session and Redis, covering the repository and service layers without
going through HTTP.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
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
from app.models.enums import UserRole, UserStatus, WalletType, WithdrawalStatus
from app.repositories.admin_read_repository import AdminReadRepository
from app.repositories.admin_session_repository import AdminSessionRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.auth_repository import AuthRepository
from app.repositories.courier_repository import CourierRepository
from app.repositories.order_repository import OrderRepository
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
        orders=OrderRepository(db),
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
    assert len(service.list_table_catalog()) == 23
    page = await service.get_table_page("users", page=1)
    assert page is not None and page.table.editable is True
    assert "phone" in page.columns
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

    await service.update_courier_profile(
        admin_id=admin.id,
        courier_user_id=courier.id,
        city_of_residence="Riyadh",
        bio="Reliable gift courier.",
        ip=None,
    )
    assert profile.city_of_residence == "Riyadh"
    assert profile.bio == "Reliable gift courier."


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


async def test_ban_and_controlled_table_edits(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    service = _admin_service(db_session, _settings(), redis_client)
    admin = await _admin(db_session)
    target = await _admin(db_session)

    await service.set_user_banned(admin_id=admin.id, user_id=target.id, banned=True, ip=None)
    banned = await service.get_user(target.id)
    assert banned.status is UserStatus.BANNED  # type: ignore[union-attr]

    await service.update_user_profile(
        admin_id=admin.id,
        user_id=target.id,
        full_name="Updated user",
        email="updated@example.test",
        ip=None,
    )
    assert banned.full_name == "Updated user"  # type: ignore[union-attr]
    assert banned.email == "updated@example.test"  # type: ignore[union-attr]

    order = await OrderRepository(db_session).create(
        customer_id=target.id,
        description="Original",
        delivery_city="Jeddah",
        longitude=39.2,
        latitude=21.5,
        delivery_date=date.today() + timedelta(days=2),
        address_note="Original note",
    )
    await service.update_order_details(
        admin_id=admin.id,
        order_id=order.id,
        description="Updated",
        delivery_city="Riyadh",
        delivery_date=date.today() + timedelta(days=3),
        delivery_address_note="Updated note",
        ip=None,
    )
    assert order.delivery_city == "Riyadh"
    assert order.description == "Updated"


async def test_admin_crud_for_users_profiles_and_new_orders(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Dashboard CRUD is audited and preserves non-draft financial history."""
    service = _admin_service(db_session, _settings(), redis_client)
    admin = await _admin(db_session)
    phone = f"+96650{uuid.uuid4().int % 10_000_000:07d}"
    customer = await service.create_user(
        admin_id=admin.id,
        phone=phone,
        full_name="Created customer",
        email="created@example.test",
        role=UserRole.CUSTOMER,
        ip=None,
    )
    await service.update_user_profile(
        admin_id=admin.id,
        user_id=customer.id,
        phone=f"+96651{uuid.uuid4().int % 10_000_000:07d}",
        full_name="Edited customer",
        email="edited@example.test",
        ip=None,
    )
    assert customer.full_name == "Edited customer"

    courier = await service.create_user(
        admin_id=admin.id,
        phone=f"+96652{uuid.uuid4().int % 10_000_000:07d}",
        full_name="Created courier",
        email=None,
        role=UserRole.COURIER,
        ip=None,
    )
    profile = await service.create_courier_profile(
        admin_id=admin.id,
        user_id=courier.id,
        city_of_residence="Jeddah",
        bio="New profile",
        identity_document="1234567890",
        identity_type="national_id",
        ip=None,
    )
    assert profile.user_id == courier.id
    await service.delete_courier_profile(admin_id=admin.id, user_id=courier.id, ip=None)
    assert await service.get_courier(courier.id) is None

    order_id = await service.create_order(
        admin_id=admin.id,
        customer_id=customer.id,
        description="Admin-created order",
        delivery_city="Jeddah",
        delivery_date=date.today() + timedelta(days=2),
        longitude=39.2,
        latitude=21.5,
        delivery_address_note="Reception",
        ip=None,
    )
    await service.delete_order(admin_id=admin.id, order_id=order_id, ip=None)
    assert await service.get_order(order_id) is None

    await service.delete_user(admin_id=admin.id, user_id=customer.id, ip=None)
    deleted = await service.get_user(customer.id)
    assert deleted is not None and deleted.deleted_at is not None


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
    auth = AdminAuthService(
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


async def test_admin_password_login_is_throttled(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    username = f"throttle-{uuid.uuid4()}"
    ip = "192.0.2.44"
    settings = make_test_settings(
        ADMIN_DASHBOARD_ENABLED=True,
        ADMIN_USERNAME=username,
        ADMIN_PASSWORD="correct-admin-password",
    )
    auth = AdminAuthService(
        users=UserRepository(db_session),
        sessions=AdminSessionRepository(db_session),
        redis=redis_client,
        settings=settings,
    )
    user_key = f"admin:login:user:{sha256_hex(username.casefold())}"
    ip_key = f"admin:login:ip:{sha256_hex(ip)}"
    try:
        for _ in range(5):
            with pytest.raises(UnauthorizedError):
                await auth.complete_login(
                    username=username,
                    password="wrong",
                    ip=ip,
                    user_agent=None,
                )
        with pytest.raises(RateLimitedError):
            await auth.complete_login(
                username=username,
                password="wrong",
                ip=ip,
                user_agent=None,
            )
    finally:
        await redis_client.delete(user_key, ip_key)
