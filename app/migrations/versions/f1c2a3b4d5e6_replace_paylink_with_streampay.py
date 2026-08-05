"""Replace Paylink payment-intent fields with StreamPay payment-link fields.

Revision ID: f1c2a3b4d5e6
Revises: 8c9d1e2f3a4b
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1c2a3b4d5e6"
down_revision: str | None = "8c9d1e2f3a4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename provider-specific intent columns and recreate their partial unique index."""
    op.drop_index("uq_payment_intents_paylink_txn", table_name="payment_intents")
    op.alter_column(
        "payment_intents",
        "paylink_transaction_no",
        new_column_name="streampay_payment_link_id",
    )
    op.alter_column("payment_intents", "paylink_url", new_column_name="streampay_payment_url")
    op.create_index(
        "uq_payment_intents_streampay_link",
        "payment_intents",
        ["streampay_payment_link_id"],
        unique=True,
        postgresql_where=sa.text("streampay_payment_link_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Restore the former column names for a rollback to the Paylink release."""
    op.drop_index("uq_payment_intents_streampay_link", table_name="payment_intents")
    op.alter_column(
        "payment_intents",
        "streampay_payment_link_id",
        new_column_name="paylink_transaction_no",
    )
    op.alter_column("payment_intents", "streampay_payment_url", new_column_name="paylink_url")
    op.create_index(
        "uq_payment_intents_paylink_txn",
        "payment_intents",
        ["paylink_transaction_no"],
        unique=True,
        postgresql_where=sa.text("paylink_transaction_no IS NOT NULL"),
    )
