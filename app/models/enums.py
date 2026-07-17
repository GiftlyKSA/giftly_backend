"""Domain enumerations, one-to-one with the native PG enums (SPEC SECTION 9).

Members are UPPER_SNAKE to match the database enum members exactly. These are the
canonical Python-side names used across services, schemas, and the state machine.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    """A user's role in the marketplace."""

    CUSTOMER = "CUSTOMER"
    COURIER = "COURIER"
    ADMIN = "ADMIN"


class UserStatus(StrEnum):
    """Account lifecycle status."""

    ACTIVE = "ACTIVE"
    BANNED = "BANNED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"


class OrderStatus(StrEnum):
    """Order lifecycle status (see the state machine in SPEC SECTION 9)."""

    NEW = "NEW"
    ASSIGNED = "ASSIGNED"
    WAITING_PAYMENT = "WAITING_PAYMENT"
    IN_PROGRESS = "IN_PROGRESS"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    DISPUTED = "DISPUTED"
    REFUNDED = "REFUNDED"


class InvoiceStatus(StrEnum):
    """Invoice lifecycle status."""

    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REFUNDED = "REFUNDED"


class PaymentPurpose(StrEnum):
    """Why a gateway payment intent exists."""

    ORDER_INVOICE = "ORDER_INVOICE"
    WALLET_TOPUP = "WALLET_TOPUP"


class PaymentIntentStatus(StrEnum):
    """Gateway-facing payment intent status."""

    NEW = "NEW"
    PAID = "PAID"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PaymentMethod(StrEnum):
    """How an invoice was funded."""

    WALLET_ONLY = "WALLET_ONLY"
    GATEWAY_ONLY = "GATEWAY_ONLY"
    SPLIT = "SPLIT"


class PromoDiscountType(StrEnum):
    """How a promo discount is expressed."""

    PERCENT = "PERCENT"
    FIXED = "FIXED"


class PromoRedemptionStatus(StrEnum):
    """Lifecycle of a promo reservation."""

    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class MediaType(StrEnum):
    """The purpose of an uploaded media object."""

    CUSTOMER_REQUEST = "CUSTOMER_REQUEST"
    DELIVERY_PROOF = "DELIVERY_PROOF"


class MessageType(StrEnum):
    """The kind of a chat message (also used for portfolio media)."""

    TEXT = "TEXT"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    SYSTEM = "SYSTEM"


class WalletType(StrEnum):
    """Wallet kind. The four SYSTEM_* wallets are seeded by migration."""

    CUSTOMER = "CUSTOMER"
    COURIER = "COURIER"
    SYSTEM_ESCROW = "SYSTEM_ESCROW"
    SYSTEM_REVENUE = "SYSTEM_REVENUE"
    SYSTEM_GATEWAY = "SYSTEM_GATEWAY"
    SYSTEM_TAX_PAYABLE = "SYSTEM_TAX_PAYABLE"


class TransactionType(StrEnum):
    """The economic meaning of a ledger entry."""

    TOPUP = "TOPUP"
    WITHDRAWAL = "WITHDRAWAL"
    ESCROW_HOLD = "ESCROW_HOLD"
    ESCROW_RELEASE = "ESCROW_RELEASE"
    PAYMENT = "PAYMENT"
    REFUND = "REFUND"
    COMMISSION = "COMMISSION"
    SERVICE_FEE = "SERVICE_FEE"
    TAX = "TAX"
    PROMO_SUBSIDY = "PROMO_SUBSIDY"


class TransactionStatus(StrEnum):
    """Ledger entry status. Only PENDING -> SETTLED|REVERSED is a permitted update."""

    PENDING = "PENDING"
    SETTLED = "SETTLED"
    REVERSED = "REVERSED"


class DeviceOs(StrEnum):
    """Device platform for push tokens."""

    IOS = "IOS"
    ANDROID = "ANDROID"


class WithdrawalStatus(StrEnum):
    """Courier withdrawal lifecycle."""

    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    PAID = "PAID"
    REJECTED = "REJECTED"


class DisputeStatus(StrEnum):
    """Dispute lifecycle and resolution outcome."""

    OPEN = "OPEN"
    RESOLVED_CUSTOMER = "RESOLVED_CUSTOMER"
    RESOLVED_COURIER = "RESOLVED_COURIER"
    RESOLVED_SPLIT = "RESOLVED_SPLIT"
