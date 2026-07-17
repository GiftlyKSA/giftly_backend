"""Shared production guard for environment-gated integration doubles.

Layer 3 of the §5.2 interlock: every Fake calls this in ``__init__`` so a fake is
structurally incapable of existing in production, not merely un-selected.
"""

from __future__ import annotations

from app.core.config import Environment


class ProductionFakeError(RuntimeError):
    """Raised when a Fake integration double is constructed in production."""


def forbid_in_production(environment: Environment, fake_name: str) -> None:
    """Raise if a fake double is being constructed under production.

    Args:
        environment: The active environment.
        fake_name: The fake's class name, for the error message.

    Raises:
        ProductionFakeError: ``environment`` is production.
    """
    if environment is Environment.PRODUCTION:
        raise ProductionFakeError(f"{fake_name} must never be constructed in production.")
