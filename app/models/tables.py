"""SQLAlchemy ORM models for every SAFE-GIFT table (SPEC SECTION 10, 13).

Models carry no business logic beyond hybrid properties. Money columns are
``Numeric`` (never float); spatial columns are PostGIS ``Geometry(Point, 4326)``;
enums are native PG enums. CHECK constraints, unique constraints, and indexes are
declared in ``__table_args__`` so the schema is defined in one place and the DB
enforces the invariants independently of the services.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Native PG enum types, created once by the baseline migration (create_type=False so
# the ORM never tries to re-create them at table-create time).
_user_role = ENUM(enums.UserRole, name="user_role", create_type=False)
_user_status = ENUM(enums.UserStatus, name="user_status", create_type=False)
_order_status = ENUM(enums.OrderStatus, name="order_status", create_type=False)
_invoice_status = ENUM(enums.InvoiceStatus, name="invoice_status", create_type=False)
_payment_purpose = ENUM(enums.PaymentPurpose, name="payment_purpose", create_type=False)
_payment_intent_status = ENUM(
    enums.PaymentIntentStatus, name="payment_intent_status", create_type=False
)
_payment_method = ENUM(enums.PaymentMethod, name="payment_method", create_type=False)
_promo_discount_type = ENUM(enums.PromoDiscountType, name="promo_discount_type", create_type=False)
_promo_redemption_status = ENUM(
    enums.PromoRedemptionStatus, name="promo_redemption_status", create_type=False
)
_media_type = ENUM(enums.MediaType, name="media_type", create_type=False)
_message_type = ENUM(enums.MessageType, name="message_type", create_type=False)
_wallet_type = ENUM(enums.WalletType, name="wallet_type", create_type=False)
_transaction_type = ENUM(enums.TransactionType, name="transaction_type", create_type=False)
_transaction_status = ENUM(enums.TransactionStatus, name="transaction_status", create_type=False)
_device_os = ENUM(enums.DeviceOs, name="device_os", create_type=False)
_withdrawal_status = ENUM(enums.WithdrawalStatus, name="withdrawal_status", create_type=False)
_dispute_status = ENUM(enums.DisputeStatus, name="dispute_status", create_type=False)

_MONEY = Numeric(12, 2)
_RATE = Numeric(6, 4)


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    """A rotating refresh token, stored only as a SHA-256 hash (SPEC SECTION 17.2 A07).

    Refresh tokens rotate on every use and belong to a ``family_id``. Presenting a
    token that has already been used (``used_at`` set) or revoked means the family is
    compromised, so the whole family is revoked and re-auth is forced (reuse
    detection). The raw token lives only in the client; the DB holds its hash.

    Note:
        This table is not in SPEC SECTION 10 — the spec mandates rotating refresh
        tokens with reuse detection but does not define their storage. A dedicated
        hashed table is the most explicit, auditable option (see DECISIONS.md).
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_tokens_hash"),
        Index("idx_refresh_tokens_family", "family_id"),
        Index("idx_refresh_tokens_user", "user_id"),
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A marketplace participant (customer, courier, or admin).

    ``rating`` is a DENORMALIZED cache of the ``ratings`` table, recomputed
    transactionally on each rating insert — never the source of truth. Soft-deleted
    on erasure; ledger rows are never removed.
    """

    __tablename__ = "users"

    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    role: Mapped[enums.UserRole] = mapped_column(_user_role, nullable=False)
    status: Mapped[enums.UserStatus] = mapped_column(
        _user_status, nullable=False, server_default=enums.UserStatus.ACTIVE.value
    )
    rating: Mapped[Decimal] = mapped_column(
        Numeric(2, 1), nullable=False, server_default=text("5.0")
    )
    rating_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("rating BETWEEN 0.0 AND 5.0", name="chk_rating_range"),
        UniqueConstraint("phone", name="uq_users_phone"),
        Index("uq_users_email", "email", unique=True, postgresql_where=text("email IS NOT NULL")),
        Index("idx_users_role_status", "role", "status"),
    )


class CourierProfile(TimestampMixin, Base):
    """Courier identity and verification state (PK == users.id).

    ``passport_id_encrypted`` / ``national_id_encrypted`` are AES-256-GCM ciphertext.
    ``identity_fingerprint`` is an HMAC blind index enabling duplicate detection
    without decryption. Only an ACTIVE, verified courier may accept or invoice.
    """

    __tablename__ = "courier_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    passport_id_encrypted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    national_id_encrypted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    identity_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    city_of_residence: Mapped[str] = mapped_column(String(100), nullable=False)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "passport_id_encrypted IS NOT NULL OR national_id_encrypted IS NOT NULL",
            name="chk_identity_present",
        ),
        Index("idx_courier_profiles_city_verified", "city_of_residence", "is_verified"),
        Index(
            "uq_courier_identity_fingerprint",
            "identity_fingerprint",
            unique=True,
            postgresql_where=text("identity_fingerprint IS NOT NULL"),
        ),
    )


class CourierPortfolio(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A courier's portfolio ("Museum") media item. Max 30 per courier (service rule)."""

    __tablename__ = "courier_portfolios"

    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[enums.MessageType] = mapped_column(
        _message_type, nullable=False, server_default=enums.MessageType.IMAGE.value
    )
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (Index("idx_courier_portfolios_courier", "courier_id", "display_order"),)


class DeviceToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A push token for one of a user's devices."""

    __tablename__ = "device_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_os: Mapped[enums.DeviceOs] = mapped_column(_device_os, nullable=False)
    token: Mapped[str] = mapped_column(String(512), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("token", name="uq_device_tokens_token"),
        Index("idx_device_tokens_user", "user_id"),
    )


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A customer's gift request, tied to a city and a delivery date <= 6 months out.

    ``description`` is intentionally NOT encrypted: no healthcare data is in scope
    (SPEC SECTION 1), so it is not Restricted. ``delivery_location`` stores lng-first.
    """

    __tablename__ = "orders"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    courier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_city: Mapped[str] = mapped_column(String(100), nullable=False)
    delivery_location: Mapped[object] = mapped_column(
        # spatial_index=False: the explicit idx_orders_location_gist below is the one
        # GIST index we want; GeoAlchemy2's auto-index would duplicate it.
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    delivery_address_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[enums.OrderStatus] = mapped_column(
        _order_status, nullable=False, server_default=enums.OrderStatus.NEW.value
    )
    total_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    commission_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    courier_payout_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "delivery_date >= CURRENT_DATE AND delivery_date <= CURRENT_DATE + INTERVAL '180 days'",
            name="chk_delivery_date_window",
        ),
        CheckConstraint(
            "total_amount >= 0 AND commission_amount >= 0 AND courier_payout_amount >= 0",
            name="chk_amounts_non_negative",
        ),
        CheckConstraint(
            "status IN ('NEW','CANCELLED') OR courier_id IS NOT NULL",
            name="chk_courier_required_after_assignment",
        ),
        Index("idx_orders_city_status", "delivery_city", "status"),
        Index("idx_orders_customer_created", "customer_id", text("created_at DESC")),
        Index(
            "idx_orders_courier_created",
            "courier_id",
            text("created_at DESC"),
            postgresql_where=text("courier_id IS NOT NULL"),
        ),
        Index(
            "idx_orders_status_delivered_at",
            "status",
            "delivered_at",
            postgresql_where=text("status = 'DELIVERED'"),
        ),
        Index("idx_orders_location_gist", "delivery_location", postgresql_using="gist"),
    )


class OrderMedia(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A media object attached to an order (customer request or delivery proof)."""

    __tablename__ = "order_media"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    media_type: Mapped[enums.MediaType] = mapped_column(_media_type, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    capture_location: Mapped[object | None] = mapped_column(
        # No spatial index in the spec index list for proof media; suppress the auto one.
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "media_type <> 'DELIVERY_PROOF' OR capture_location IS NOT NULL",
            name="chk_proof_has_location",
        ),
        Index("idx_order_media_order_type", "order_id", "media_type"),
    )


class Promo(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A discount code. ``code`` is stored NORMALIZED (strip().upper()).

    ``used_count`` is a DENORMALIZED counter maintained only by the atomic
    conditional UPDATE in SPEC SECTION 12.3; it counts RESERVED + CONSUMED.
    """

    __tablename__ = "promos"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    discount_type: Mapped[enums.PromoDiscountType] = mapped_column(
        _promo_discount_type, nullable=False
    )
    percent_value: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    fixed_amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    max_discount_amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    min_order_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    max_total_usages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_usages_per_user: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("code = upper(code) AND code = btrim(code)", name="chk_promo_code_upper"),
        CheckConstraint("char_length(code) BETWEEN 3 AND 32", name="chk_promo_code_len"),
        CheckConstraint(
            "(discount_type='PERCENT' AND percent_value IS NOT NULL AND fixed_amount IS NULL "
            "AND percent_value > 0 AND percent_value <= 100) "
            "OR (discount_type='FIXED' AND fixed_amount IS NOT NULL AND percent_value IS NULL "
            "AND fixed_amount > 0)",
            name="chk_promo_value_by_type",
        ),
        CheckConstraint(
            "used_count >= 0 AND (max_total_usages IS NULL OR used_count <= max_total_usages) "
            "AND max_usages_per_user >= 1",
            name="chk_promo_usage_bounds",
        ),
        CheckConstraint(
            "starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at",
            name="chk_promo_window",
        ),
        UniqueConstraint("code", name="uq_promos_code"),
        Index("idx_promos_active_window", "is_active", "starts_at", "ends_at"),
    )


class Invoice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A courier-authored, itemised, priced invoice for an order.

    Every stored amount is the OUTPUT of the pricing engine (SPEC SECTION 11). The
    DB CHECKs re-verify the arithmetic independently. Only one invoice per order may
    be DRAFT/ISSUED/PAID at a time (partial unique index).
    """

    __tablename__ = "invoices"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    issued_by_courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    status: Mapped[enums.InvoiceStatus] = mapped_column(
        _invoice_status, nullable=False, server_default=enums.InvoiceStatus.DRAFT.value
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'SAR'"))
    items_net_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    courier_fee_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    service_fee_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    net_after_discount_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    tax_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=text("0.00"))
    total_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    promo_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("promos.id", ondelete="RESTRICT"), nullable=True
    )
    promo_code_snapshot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payment_method: Mapped[enums.PaymentMethod | None] = mapped_column(
        _payment_method, nullable=True
    )
    amount_from_wallet: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    amount_from_gateway: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    pricing_breakdown: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipt_email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "items_net_amount >= 0 AND courier_fee_amount >= 0 AND service_fee_amount >= 0 "
            "AND discount_amount >= 0 AND tax_amount >= 0 AND total_amount >= 0",
            name="chk_invoice_amounts_non_negative",
        ),
        CheckConstraint("status = 'DRAFT' OR total_amount > 0", name="chk_invoice_total_positive"),
        CheckConstraint(
            "net_after_discount_amount = items_net_amount + courier_fee_amount "
            "+ service_fee_amount - discount_amount",
            name="chk_invoice_net_math",
        ),
        CheckConstraint(
            "total_amount = net_after_discount_amount + tax_amount",
            name="chk_invoice_total_math",
        ),
        CheckConstraint(
            "(promo_id IS NULL AND discount_amount = 0) "
            "OR (promo_id IS NOT NULL AND discount_amount > 0)",
            name="chk_invoice_promo_pairing",
        ),
        Index("idx_invoices_order", "order_id"),
        Index(
            "uq_invoices_one_active_per_order",
            "order_id",
            unique=True,
            postgresql_where=text("status IN ('DRAFT','ISSUED','PAID')"),
        ),
        Index(
            "idx_invoices_receipt_pending",
            "id",
            postgresql_where=text("status='PAID' AND receipt_email_sent_at IS NULL"),
        ),
        Index(
            "idx_invoices_status_expires",
            "status",
            "expires_at",
            postgresql_where=text("status='ISSUED'"),
        ),
        Index("idx_invoices_promo", "promo_id", postgresql_where=text("promo_id IS NOT NULL")),
    )


class InvoiceItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One priced line of an invoice; frozen once the parent invoice is ISSUED."""

    __tablename__ = "invoice_items"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    unit_price_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(_RATE, nullable=False)
    line_net_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    line_discount_amount: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    line_taxable_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    line_tax_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    line_total_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)

    __table_args__ = (
        UniqueConstraint("invoice_id", "position", name="uq_invoice_item_position"),
        CheckConstraint("quantity BETWEEN 1 AND 999", name="chk_item_quantity"),
        CheckConstraint("unit_price_amount > 0", name="chk_item_unit_price"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 1", name="chk_item_tax_rate"),
        CheckConstraint(
            "line_net_amount = unit_price_amount * quantity "
            "AND line_taxable_amount = line_net_amount - line_discount_amount "
            "AND line_total_amount = line_taxable_amount + line_tax_amount",
            name="chk_item_line_math",
        ),
        Index("idx_invoice_items_invoice", "invoice_id", "position"),
    )


class PromoRedemption(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reservation/consumption of a promo against one invoice."""

    __tablename__ = "promo_redemptions"

    promo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("promos.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False
    )
    discount_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    status: Mapped[enums.PromoRedemptionStatus] = mapped_column(
        _promo_redemption_status,
        nullable=False,
        server_default=enums.PromoRedemptionStatus.RESERVED.value,
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("promo_id", "invoice_id", name="uq_promo_redemption_invoice"),
        CheckConstraint("discount_amount > 0", name="chk_redemption_amount_positive"),
        Index("idx_promo_redemptions_promo_user", "promo_id", "user_id", "status"),
        Index("idx_promo_redemptions_invoice", "invoice_id"),
    )


class PaymentIntent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The single gateway-facing record for an invoice remainder OR a top-up.

    Unifying both purposes means the webhook does ONE lookup and dispatches on
    ``purpose`` — the ambiguity that produces double-credits is designed out.
    """

    __tablename__ = "payment_intents"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    purpose: Mapped[enums.PaymentPurpose] = mapped_column(_payment_purpose, nullable=False)
    amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'SAR'"))
    status: Mapped[enums.PaymentIntentStatus] = mapped_column(
        _payment_intent_status,
        nullable=False,
        server_default=enums.PaymentIntentStatus.NEW.value,
    )
    checkout_provider: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'STREAMPAY'")
    )
    streampay_payment_link_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    streampay_payment_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reference_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_intent_amount_positive"),
        CheckConstraint(
            "(purpose='ORDER_INVOICE' AND reference_invoice_id IS NOT NULL) "
            "OR (purpose='WALLET_TOPUP' AND reference_invoice_id IS NULL)",
            name="chk_intent_purpose_reference",
        ),
        Index(
            "uq_payment_intents_streampay_link",
            "streampay_payment_link_id",
            unique=True,
            postgresql_where=text("streampay_payment_link_id IS NOT NULL"),
        ),
        Index("idx_payment_intents_user_created", "user_id", text("created_at DESC")),
        Index(
            "idx_payment_intents_status_expires",
            "status",
            "expires_at",
            postgresql_where=text("status='NEW'"),
        ),
        Index(
            "idx_payment_intents_invoice",
            "reference_invoice_id",
            postgresql_where=text("reference_invoice_id IS NOT NULL"),
        ),
    )


class WalletTopup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A customer wallet top-up, bounded 100–20,000 SAR by a DB CHECK."""

    __tablename__ = "wallet_topups"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False
    )
    payment_intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_intents.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)

    __table_args__ = (
        CheckConstraint("amount >= 100.00 AND amount <= 20000.00", name="chk_topup_bounds"),
        UniqueConstraint("payment_intent_id", name="uq_wallet_topups_intent"),
        Index("idx_wallet_topups_user_created", "user_id", text("created_at DESC")),
    )


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The chat thread for an order. The preview is ENCRYPTED (SPEC SECTION 10)."""

    __tablename__ = "conversations"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    last_message_preview_encrypted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_message_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    customer_unread_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    courier_unread_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        UniqueConstraint("order_id", name="uq_conversations_order"),
        Index(
            "idx_conversations_customer_inbox",
            "customer_id",
            text("last_message_timestamp DESC"),
        ),
        Index(
            "idx_conversations_courier_inbox",
            "courier_id",
            text("last_message_timestamp DESC"),
        ),
    )


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An append-only chat message; ``content_encrypted`` is AES-256-GCM."""

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    message_type: Mapped[enums.MessageType] = mapped_column(
        _message_type, nullable=False, server_default=enums.MessageType.TEXT.value
    )
    content_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "idx_messages_conversation_keyset",
            "conversation_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )


class Wallet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A money account. ``available = balance - held_balance`` (SPEC SECTION 10)."""

    __tablename__ = "wallets"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    type: Mapped[enums.WalletType] = mapped_column(_wallet_type, nullable=False)
    balance: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, server_default=text("0.00"))
    held_balance: Mapped[Decimal] = mapped_column(
        _MONEY, nullable=False, server_default=text("0.00")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'SAR'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(
            "(type IN ('CUSTOMER','COURIER') AND user_id IS NOT NULL) "
            "OR (type IN ('SYSTEM_ESCROW','SYSTEM_REVENUE','SYSTEM_GATEWAY','SYSTEM_TAX_PAYABLE') "
            "AND user_id IS NULL)",
            name="chk_user_wallet_pairing",
        ),
        CheckConstraint(
            "type IN ('SYSTEM_GATEWAY','SYSTEM_REVENUE') OR (balance >= 0 AND held_balance >= 0)",
            name="chk_balance_non_negative",
        ),
        Index(
            "uq_wallets_user", "user_id", unique=True, postgresql_where=text("user_id IS NOT NULL")
        ),
        Index(
            "uq_wallets_one_escrow",
            "type",
            unique=True,
            postgresql_where=text("type='SYSTEM_ESCROW'"),
        ),
        Index(
            "uq_wallets_one_revenue",
            "type",
            unique=True,
            postgresql_where=text("type='SYSTEM_REVENUE'"),
        ),
        Index(
            "uq_wallets_one_gateway",
            "type",
            unique=True,
            postgresql_where=text("type='SYSTEM_GATEWAY'"),
        ),
        Index(
            "uq_wallets_one_tax",
            "type",
            unique=True,
            postgresql_where=text("type='SYSTEM_TAX_PAYABLE'"),
        ),
    )


class Transaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """APPEND-ONLY ledger entry — the only truth about money (SPEC SECTION 10).

    UPDATE and DELETE are forbidden by trigger; the single exception is the status
    transition PENDING -> SETTLED|REVERSED. Every movement writes >= 2 rows sharing
    one ``correlation_id`` whose signed amounts sum to zero.
    """

    __tablename__ = "transactions"

    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    type: Mapped[enums.TransactionType] = mapped_column(_transaction_type, nullable=False)
    status: Mapped[enums.TransactionStatus] = mapped_column(
        _transaction_status, nullable=False, server_default=enums.TransactionStatus.SETTLED.value
    )
    reference_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True
    )
    reference_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=True
    )
    reference_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_intents.id", ondelete="RESTRICT"), nullable=True
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint("amount <> 0", name="chk_amount_non_zero"),
        Index(
            "idx_transactions_wallet_created",
            "wallet_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index("idx_transactions_correlation", "correlation_id"),
        Index(
            "idx_transactions_order",
            "reference_order_id",
            postgresql_where=text("reference_order_id IS NOT NULL"),
        ),
        Index(
            "uq_transactions_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )


class Withdrawal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A courier payout request; ``iban_encrypted`` is AES-256-GCM."""

    __tablename__ = "withdrawals"

    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    iban_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)
    iban_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    status: Mapped[enums.WithdrawalStatus] = mapped_column(
        _withdrawal_status, nullable=False, server_default=enums.WithdrawalStatus.REQUESTED.value
    )
    processed_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("amount >= 50.00", name="chk_withdrawal_min"),
        Index("idx_withdrawals_courier_status", "courier_id", "status"),
        Index("idx_withdrawals_status_created", "status", "created_at"),
        Index(
            "uq_withdrawals_courier_idempotency",
            "courier_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )


class Rating(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A one-per-rater rating on a completed order."""

    __tablename__ = "ratings"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    rater_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    rated_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        CheckConstraint("score BETWEEN 1 AND 5", name="chk_score_range"),
        CheckConstraint("rater_id <> rated_user_id", name="chk_no_self_rating"),
        UniqueConstraint("order_id", "rater_id", name="uq_ratings_order_rater"),
        Index("idx_ratings_rated_user", "rated_user_id"),
    )


class Dispute(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A frozen-escrow dispute awaiting admin resolution."""

    __tablename__ = "disputes"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False
    )
    raised_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[enums.DisputeStatus] = mapped_column(
        _dispute_status, nullable=False, server_default=enums.DisputeStatus.OPEN.value
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("order_id", name="uq_disputes_order"),
        Index("idx_disputes_status_created", "status", "created_at"),
    )


class AdminSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A server-side admin dashboard session; stores only the token hash."""

    __tablename__ = "admin_sessions"

    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("session_token_hash", name="uq_admin_sessions_token_hash"),
        Index("idx_admin_sessions_admin_expires", "admin_user_id", "expires_at"),
    )


class OtpAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An OTP verify attempt, keyed by a phone HMAC (never plaintext)."""

    __tablename__ = "otp_attempts"

    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    was_successful: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An append-only audit record; ``metadata`` is scrubbed of Restricted data."""

    __tablename__ = "audit_logs"

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    audit_metadata: Mapped[dict[str, object] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    __table_args__ = (
        Index("idx_audit_logs_actor_created", "actor_user_id", text("created_at DESC")),
        Index("idx_audit_logs_entity", "entity_type", "entity_id"),
    )
