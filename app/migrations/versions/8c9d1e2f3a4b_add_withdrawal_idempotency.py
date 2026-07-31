"""Add courier-scoped withdrawal idempotency keys.

Revision ID: 8c9d1e2f3a4b
Revises: dadcdbda923c
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8c9d1e2f3a4b"
down_revision: str | None = "dadcdbda923c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add an optional key and a unique partial index for safe retries."""
    op.add_column("withdrawals", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.create_index(
        "uq_withdrawals_courier_idempotency",
        "withdrawals",
        ["courier_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove withdrawal idempotency storage."""
    op.drop_index("uq_withdrawals_courier_idempotency", table_name="withdrawals")
    op.drop_column("withdrawals", "idempotency_key")
