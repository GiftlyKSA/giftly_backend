"""Domain exceptions and their stable error codes (SPEC SECTION 8.14-16).

Domain code raises these; the global handler maps them to the §8.16 error envelope.
The ``code`` is a stable machine string documented in docs/conventions.md; the
``message`` is safe for end-user display. Clients never see stack traces or internals.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for expected, mappable domain failures.

    Attributes:
        code: Stable machine-readable error code.
        message: End-user-safe message.
        status_code: HTTP status the global handler should emit.
    """

    code: str = "INTERNAL_ERROR"
    message: str = "Something went wrong."
    status_code: int = 400

    def __init__(self, message: str | None = None) -> None:
        """Allow a per-instance message override while keeping the class default."""
        super().__init__(message or self.message)
        if message:
            self.message = message


class NotFoundError(DomainError):
    """The resource does not exist, or the actor has no relationship to it."""

    code = "NOT_FOUND"
    message = "The requested resource was not found."
    status_code = 404


class ForbiddenError(DomainError):
    """The actor is related to the resource but not permitted this action."""

    code = "FORBIDDEN"
    message = "You do not have permission to perform this action."
    status_code = 403


class UnauthorizedError(DomainError):
    """Authentication is missing or invalid."""

    code = "UNAUTHORIZED"
    message = "Authentication is required."
    status_code = 401


class InvalidStateTransitionError(DomainError):
    """A state machine transition is not legal from the current state."""

    code = "INVALID_STATE_TRANSITION"
    message = "This action is not allowed in the current state."
    status_code = 409


class ConflictError(DomainError):
    """A concurrent operation won a lock first, or a uniqueness rule was hit."""

    code = "CONFLICT"
    message = "The resource was modified by another operation."
    status_code = 409


class OrderAlreadyAssignedError(ConflictError):
    """Another courier won the accept lock first."""

    code = "ORDER_ALREADY_ASSIGNED"
    message = "This order has already been assigned to another courier."


class BadRequestError(DomainError):
    """A malformed or out-of-bounds request value (400)."""

    code = "BAD_REQUEST"
    message = "The request could not be processed."
    status_code = 400


class ValidationDomainError(DomainError):
    """A semantically invalid request that passed schema validation."""

    code = "VALIDATION_ERROR"
    message = "The request could not be processed."
    status_code = 422


class InsufficientFundsError(DomainError):
    """The wallet's available balance cannot cover the requested debit."""

    code = "INSUFFICIENT_FUNDS"
    message = "Your available balance is insufficient for this operation."
    status_code = 409


class InvalidWebhookSignatureError(DomainError):
    """A gateway webhook failed HMAC verification over its raw body."""

    code = "INVALID_SIGNATURE"
    message = "The webhook signature is invalid."
    status_code = 401


class PaymentAmountMismatchError(DomainError):
    """A webhook's amount does not match the payment intent it claims to settle."""

    code = "PAYMENT_AMOUNT_MISMATCH"
    message = "The payment amount does not match the intent."
    status_code = 400


class RateLimitedError(DomainError):
    """The actor exceeded a rate limit; carries a retry-after hint."""

    code = "RATE_LIMITED"
    message = "Too many requests. Please try again later."
    status_code = 429

    def __init__(self, retry_after_seconds: int, message: str | None = None) -> None:
        """Record the retry-after hint for the ``Retry-After`` header."""
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PromoError(ValidationDomainError):
    """Base for promo validation failures (SPEC SECTION 12.2); all are 422."""


class PromoNotFoundError(PromoError):
    """No promo exists for the normalized code."""

    code = "PROMO_NOT_FOUND"
    message = "This promo code is not recognised."


class PromoInactiveError(PromoError):
    """The promo exists but is deactivated."""

    code = "PROMO_INACTIVE"
    message = "This promo code is not active."


class PromoNotStartedError(PromoError):
    """The promo's start time is in the future."""

    code = "PROMO_NOT_STARTED"
    message = "This promo code is not available yet."


class PromoExpiredError(PromoError):
    """The promo's end time has passed."""

    code = "PROMO_EXPIRED"
    message = "This promo code has expired."


class PromoMinOrderNotMetError(PromoError):
    """The order value is below the promo's minimum."""

    code = "PROMO_MIN_ORDER_NOT_MET"
    message = "Your order does not meet this promo's minimum amount."


class PromoUsageExceededError(PromoError):
    """The promo's global usage cap has been reached."""

    code = "PROMO_USAGE_EXCEEDED"
    message = "This promo code is no longer available."


class PromoUserLimitReachedError(PromoError):
    """This user has already used the promo the maximum number of times."""

    code = "PROMO_USER_LIMIT_REACHED"
    message = "You have already used this promo code."
