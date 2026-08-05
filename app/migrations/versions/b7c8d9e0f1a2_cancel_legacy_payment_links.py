"""Cancel open legacy payment links during the StreamPay cutover.

Revision ID: b7c8d9e0f1a2
Revises: f1c2a3b4d5e6
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "f1c2a3b4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Release legacy invoice holds and prevent obsolete checkout URLs from being reused."""
    op.add_column(
        "payment_intents",
        sa.Column("checkout_provider", sa.String(length=20), server_default="LEGACY", nullable=False),
    )
    op.execute(
        """
        UPDATE wallets AS wallet
        SET held_balance = GREATEST(0, wallet.held_balance - legacy_hold.amount)
        FROM (
            SELECT intent.user_id, SUM(invoice.amount_from_wallet) AS amount
            FROM payment_intents AS intent
            JOIN invoices AS invoice ON invoice.id = intent.reference_invoice_id
            WHERE intent.status = 'NEW'
              AND intent.purpose = 'ORDER_INVOICE'
              AND intent.checkout_provider = 'LEGACY'
            GROUP BY intent.user_id
        ) AS legacy_hold
        WHERE wallet.user_id = legacy_hold.user_id
        """
    )
    op.execute(
        """
        UPDATE payment_intents
        SET status = 'CANCELLED',
            failure_reason = 'Legacy payment link replaced by StreamPay',
            streampay_payment_link_id = NULL,
            streampay_payment_url = NULL
        WHERE status = 'NEW' AND checkout_provider = 'LEGACY'
        """
    )
    op.alter_column("payment_intents", "checkout_provider", server_default="STREAMPAY")


def downgrade() -> None:
    """Drop the provider marker; cancelled legacy links intentionally stay cancelled."""
    op.drop_column("payment_intents", "checkout_provider")
