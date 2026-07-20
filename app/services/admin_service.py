"""Admin dashboard operations (SPEC SECTION 18.3).

The dashboard calls these methods; it never queries the DB directly. Reads aggregate
through the read repository; the mutations here are the ones that move no money
(courier verification, promo management, user ban/unban, and the audited reveal of a
Restricted identity/IBAN). Every mutation writes an audit row. Money-moving admin
actions (dispute payout resolution, withdrawal settlement) belong to the ledger
service and are intentionally not performed here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.crypto import build_aad, build_cipher
from app.core.exceptions import NotFoundError
from app.models import CourierProfile, Withdrawal
from app.models.enums import PromoDiscountType, UserStatus
from app.repositories.admin_read_repository import AdminReadRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.auth_repository import AuthRepository
from app.repositories.courier_repository import CourierRepository
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

    # --- Promo management (no money) ------------------------------------------

    async def create_promo(
        self,
        *,
        admin_id: uuid.UUID,
        code: str,
        description: str,
        discount_type: PromoDiscountType,
        percent_value: Decimal | None,
        fixed_amount: Decimal | None,
        max_discount_amount: Decimal | None,
        min_order_amount: Decimal,
        max_total_usages: int | None,
        max_usages_per_user: int,
        ip: str | None,
    ) -> uuid.UUID:
        """Create a promo (normalized code) and audit it; returns the new promo id."""
        promo = await self._promos.create(
            code=code,
            description=description,
            discount_type=discount_type,
            percent_value=percent_value,
            fixed_amount=fixed_amount,
            max_discount_amount=max_discount_amount,
            min_order_amount=min_order_amount,
            max_total_usages=max_total_usages,
            max_usages_per_user=max_usages_per_user,
            created_by_admin_id=admin_id,
        )
        await self._audit.record(
            actor_user_id=admin_id,
            action="PROMO_CREATE",
            entity_type="promos",
            entity_id=promo.id,
            ip_address=ip,
            metadata={"code": promo.code},
        )
        return promo.id

    async def set_promo_active(
        self, *, admin_id: uuid.UUID, promo_id: uuid.UUID, active: bool, ip: str | None
    ) -> None:
        """Activate or deactivate a promo and audit it."""
        promo = await self._promos.get(promo_id)
        if promo is None:
            raise NotFoundError("Promo not found.")
        await self._promos.set_active(promo, is_active=active)
        await self._audit.record(
            actor_user_id=admin_id,
            action="PROMO_ACTIVATE" if active else "PROMO_DEACTIVATE",
            entity_type="promos",
            entity_id=promo_id,
            ip_address=ip,
        )
