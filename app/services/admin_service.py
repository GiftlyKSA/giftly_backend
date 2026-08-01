"""Admin dashboard operations (SPEC SECTION 18.3).

The dashboard calls these methods; it never queries the DB directly. Reads aggregate
through the read repository. The only dashboard edits are safe fields on users,
courier profiles, and pre-payment orders; every mutation writes an audit row.
Money-moving admin actions (dispute payout resolution, withdrawal settlement) belong
to the ledger service and are intentionally not performed here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.crypto import build_aad, build_cipher
from app.core.exceptions import ConflictError, NotFoundError, ValidationDomainError
from app.models import CourierProfile, Withdrawal
from app.models.enums import OrderStatus, UserStatus
from app.repositories.admin_read_repository import (
    AdminReadRepository,
    AdminTableInfo,
    AdminTablePage,
)
from app.repositories.audit_repository import AuditRepository
from app.repositories.auth_repository import AuthRepository
from app.repositories.courier_repository import CourierRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.promo_repository import PromoRepository
from app.repositories.user_repository import UserRepository


@dataclass(frozen=True)
class Overview:
    """The dashboard landing summary."""

    order_counts: dict[str, int]
    open_disputes: int
    pending_withdrawals: int
    system_balances: dict[str, Decimal]


class AdminService:
    """Read aggregates and non-money admin mutations, each audited."""

    def __init__(
        self,
        *,
        reads: AdminReadRepository,
        users: UserRepository,
        couriers: CourierRepository,
        orders: OrderRepository,
        promos: PromoRepository,
        audit: AuditRepository,
        auth_repo: AuthRepository,
        redis: Redis,
        settings: Settings,
    ) -> None:
        """Wire the repositories, Redis, and settings the admin operations need."""
        self._reads = reads
        self._users = users
        self._couriers = couriers
        self._orders = orders
        self._promos = promos
        self._audit = audit
        self._auth_repo = auth_repo
        self._redis = redis
        self._settings = settings

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    async def overview(self) -> Overview:
        """Aggregate the landing-page counters and system balances."""
        return Overview(
            order_counts=await self._reads.order_counts_by_status(),
            open_disputes=await self._reads.open_dispute_count(),
            pending_withdrawals=await self._reads.pending_withdrawal_count(),
            system_balances=await self._reads.system_wallet_balances(),
        )

    # --- Read passthroughs (the dashboard never touches a repository directly) --

    async def list_pending_couriers(self) -> list[CourierProfile]:
        """Return courier profiles awaiting verification."""
        return await self._couriers.list_pending()

    async def get_courier(self, courier_id: uuid.UUID) -> CourierProfile | None:
        """Return a courier profile by id."""
        return await self._couriers.get(courier_id)

    async def get_user(self, user_id: uuid.UUID) -> object | None:
        """Return a user by id."""
        return await self._users.get(user_id)

    async def list_orders(self) -> list[object]:
        """Return recent orders."""
        return list(await self._reads.list_orders())

    async def get_order(self, order_id: uuid.UUID) -> object | None:
        """Return an order by id."""
        return await self._reads.get_order(order_id)

    async def list_invoices(self) -> list[object]:
        """Return recent invoices."""
        return list(await self._reads.list_invoices())

    async def get_invoice(self, invoice_id: uuid.UUID) -> object | None:
        """Return an invoice by id."""
        return await self._reads.get_invoice(invoice_id)

    async def list_promos(self) -> list[object]:
        """Return promos."""
        return list(await self._promos.list_all())

    async def get_promo(self, promo_id: uuid.UUID) -> object | None:
        """Return a promo by id."""
        return await self._promos.get(promo_id)

    async def list_promo_redemptions(self, promo_id: uuid.UUID) -> list[object]:
        """Return a promo's redemptions."""
        return list(await self._promos.list_redemptions(promo_id))

    async def list_disputes(self) -> list[object]:
        """Return disputes."""
        return list(await self._reads.list_disputes())

    async def get_dispute(self, dispute_id: uuid.UUID) -> object | None:
        """Return a dispute by id."""
        return await self._reads.get_dispute(dispute_id)

    async def list_withdrawals(self) -> list[object]:
        """Return withdrawals."""
        return list(await self._reads.list_withdrawals())

    async def get_withdrawal(self, withdrawal_id: uuid.UUID) -> Withdrawal | None:
        """Return a withdrawal by id."""
        return await self._reads.get_withdrawal(withdrawal_id)

    async def list_wallets(self) -> list[object]:
        """Return system and user wallets."""
        return list(await self._reads.list_wallets())

    async def get_wallet(self, wallet_id: uuid.UUID) -> object | None:
        """Return a wallet by id."""
        return await self._reads.get_wallet(wallet_id)

    async def list_topups(self) -> list[object]:
        """Return wallet top-up intents."""
        return list(await self._reads.list_topups())

    async def list_audit_logs(self, limit: int = 100) -> list[object]:
        """Return recent audit-log entries."""
        return list(await self._audit.list_recent(limit=limit))

    def list_table_catalog(self) -> list[AdminTableInfo]:
        """Return every application table available through the read-only browser."""
        return self._reads.list_table_catalog()

    async def get_table_page(self, table_name: str, *, page: int) -> AdminTablePage | None:
        """Return a bounded redacted table-browser page."""
        return await self._reads.list_table_page(table_name, page=page)

    # --- Courier verification (no money) --------------------------------------

    async def verify_courier(
        self,
        *,
        admin_id: uuid.UUID,
        courier_user_id: uuid.UUID,
        approve: bool,
        note: str | None,
        ip: str | None,
    ) -> CourierProfile:
        """Approve or reject a courier's verification and audit the decision."""
        profile = await self._couriers.get(courier_user_id)
        if profile is None:
            raise NotFoundError("Courier not found.")
        await self._couriers.set_verified(
            profile, is_verified=approve, admin_id=admin_id, when=self._now()
        )
        user = await self._users.get(courier_user_id)
        if user is not None and approve and user.status is UserStatus.PENDING_VERIFICATION:
            await self._users.set_status(user, UserStatus.ACTIVE)
        await self._audit.record(
            actor_user_id=admin_id,
            action="COURIER_VERIFY" if approve else "COURIER_REJECT",
            entity_type="courier_profiles",
            entity_id=courier_user_id,
            ip_address=ip,
            metadata={"note": note} if note else None,
        )
        return profile

    async def reveal_identity(
        self, *, admin_id: uuid.UUID, courier_user_id: uuid.UUID, ip: str | None
    ) -> dict[str, str | None]:
        """Decrypt a courier's identity documents (call only after step-up); audited."""
        profile = await self._couriers.get(courier_user_id)
        if profile is None:
            raise NotFoundError("Courier not found.")
        cipher = build_cipher(
            self._settings.encryption_keys(), self._settings.FIELD_ENCRYPTION_KEY_VERSION
        )
        result: dict[str, str | None] = {"national_id": None, "passport_id": None}
        if profile.national_id_encrypted:
            result["national_id"] = cipher.decrypt(
                profile.national_id_encrypted,
                build_aad("courier_profiles", "national_id", str(courier_user_id)),
            )
        if profile.passport_id_encrypted:
            result["passport_id"] = cipher.decrypt(
                profile.passport_id_encrypted,
                build_aad("courier_profiles", "passport_id", str(courier_user_id)),
            )
        await self._audit.record(
            actor_user_id=admin_id,
            action="IDENTITY_REVEAL",
            entity_type="courier_profiles",
            entity_id=courier_user_id,
            ip_address=ip,
        )
        return result

    async def reveal_iban(
        self, *, admin_id: uuid.UUID, withdrawal: Withdrawal, ip: str | None
    ) -> str:
        """Decrypt a withdrawal IBAN (call only after step-up); audited."""
        cipher = build_cipher(
            self._settings.encryption_keys(), self._settings.FIELD_ENCRYPTION_KEY_VERSION
        )
        iban = cipher.decrypt(
            withdrawal.iban_encrypted, build_aad("withdrawals", "iban", str(withdrawal.id))
        )
        await self._audit.record(
            actor_user_id=admin_id,
            action="IBAN_REVEAL",
            entity_type="withdrawals",
            entity_id=withdrawal.id,
            ip_address=ip,
        )
        return iban

    # --- User moderation (no money) -------------------------------------------

    async def set_user_banned(
        self, *, admin_id: uuid.UUID, user_id: uuid.UUID, banned: bool, ip: str | None
    ) -> None:
        """Ban or unban a user, revoke their live sessions, and audit it.

        A ban must end access immediately (audit SEC-1): every live refresh token is
        revoked, and a Redis flag outliving the access-token TTL kills the tokens
        already in the wild — ``require_auth`` checks it on every request.
        """
        user = await self._users.get(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        await self._users.set_status(user, UserStatus.BANNED if banned else UserStatus.ACTIVE)
        banned_key = f"auth:banned:{user_id}"
        if banned:
            await self._auth_repo.revoke_all_for_user(user_id, datetime.now(UTC))
            await self._redis.set(
                banned_key, "1", ex=self._settings.JWT_ACCESS_TTL_MINUTES * 60 + 60
            )
        else:
            await self._redis.delete(banned_key)
        await self._audit.record(
            actor_user_id=admin_id,
            action="USER_BAN" if banned else "USER_UNBAN",
            entity_type="users",
            entity_id=user_id,
            ip_address=ip,
        )

    async def update_user_profile(
        self,
        *,
        admin_id: uuid.UUID,
        user_id: uuid.UUID,
        full_name: str | None,
        email: str | None,
        ip: str | None,
    ) -> None:
        """Update a user's display name/email without changing role or credentials."""
        user = await self._users.get(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        if email:
            owner = await self._users.get_by_email(email)
            if owner is not None and owner.id != user_id:
                raise ConflictError("That email is already used by another user.")
        await self._users.update_admin_profile(user, full_name=full_name, email=email)
        await self._audit.record(
            actor_user_id=admin_id,
            action="USER_PROFILE_UPDATE",
            entity_type="users",
            entity_id=user_id,
            ip_address=ip,
        )

    async def update_courier_profile(
        self,
        *,
        admin_id: uuid.UUID,
        courier_user_id: uuid.UUID,
        city_of_residence: str,
        bio: str | None,
        ip: str | None,
    ) -> None:
        """Update a courier's public profile fields and record the administrator action."""
        profile = await self._couriers.get(courier_user_id)
        if profile is None:
            raise NotFoundError("Courier not found.")
        if not city_of_residence:
            raise ValidationDomainError("City is required.")
        await self._couriers.update_admin_profile(
            profile, city_of_residence=city_of_residence, bio=bio
        )
        await self._audit.record(
            actor_user_id=admin_id,
            action="COURIER_PROFILE_UPDATE",
            entity_type="courier_profiles",
            entity_id=courier_user_id,
            ip_address=ip,
        )

    async def update_order_details(
        self,
        *,
        admin_id: uuid.UUID,
        order_id: uuid.UUID,
        description: str | None,
        delivery_city: str,
        delivery_date: date,
        delivery_address_note: str | None,
        ip: str | None,
    ) -> None:
        """Update non-financial order details before payment can make them contractual."""
        order = await self._orders.lock(order_id)
        if order is None:
            raise NotFoundError("Order not found.")
        if order.status not in (OrderStatus.NEW, OrderStatus.ASSIGNED):
            raise ConflictError("Only NEW or ASSIGNED orders may have delivery details edited.")
        if not delivery_city:
            raise ValidationDomainError("Delivery city is required.")
        today = date.today()
        if not today <= delivery_date <= today + timedelta(days=180):
            raise ValidationDomainError("Delivery date must be within the next 180 days.")
        await self._orders.update_admin_details(
            order,
            description=description,
            delivery_city=delivery_city,
            delivery_date=delivery_date,
            delivery_address_note=delivery_address_note,
        )
        await self._audit.record(
            actor_user_id=admin_id,
            action="ORDER_DETAILS_UPDATE",
            entity_type="orders",
            entity_id=order_id,
            ip_address=ip,
        )
