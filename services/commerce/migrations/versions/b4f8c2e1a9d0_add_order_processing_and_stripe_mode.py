"""add_order_processing_and_stripe_mode

Revision ID: b4f8c2e1a9d0
Revises: e2b83a741f90
Create Date: 2026-11-06 16:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4f8c2e1a9d0"
down_revision: str | Sequence[str] | None = "e2b83a741f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: добавление expires_at, payment_processing/expired для orders и mode для stripe_checkout_sessions."""
    # 1. orders: добавяне на expires_at и разширяване на статусите
    op.add_column(
        "orders",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("ck_orders_status_valid", "orders", type_="check")
    op.create_check_constraint(
        "ck_orders_status_valid",
        "orders",
        "status IN ('pending_payment', 'payment_processing', 'paid', 'cancelled', 'expired')",
    )

    # 2. stripe_checkout_sessions: добавяне на mode (test/live) и разширяване на статусите с 'processing'
    op.add_column(
        "stripe_checkout_sessions",
        sa.Column(
            "mode",
            sa.String(length=10),
            nullable=False,
            server_default="test",
        ),
    )
    op.drop_constraint(
        "ck_stripe_checkout_sessions_status_valid",
        "stripe_checkout_sessions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_stripe_checkout_sessions_status_valid",
        "stripe_checkout_sessions",
        "status IN ('initiated', 'open', 'processing', 'completed', 'cancelling', 'expired', 'failed')",
    )
    op.create_check_constraint(
        "ck_stripe_checkout_sessions_mode_valid",
        "stripe_checkout_sessions",
        "mode IN ('test', 'live')",
    )


def downgrade() -> None:
    """Downgrade schema: премахване на mode, expires_at и връщане на старите check ограничения."""
    # 1. stripe_checkout_sessions
    op.drop_constraint(
        "ck_stripe_checkout_sessions_mode_valid",
        "stripe_checkout_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_stripe_checkout_sessions_status_valid",
        "stripe_checkout_sessions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_stripe_checkout_sessions_status_valid",
        "stripe_checkout_sessions",
        "status IN ('initiated', 'open', 'completed', 'cancelling', 'expired', 'failed')",
    )
    op.drop_column("stripe_checkout_sessions", "mode")

    # 2. orders
    op.drop_constraint("ck_orders_status_valid", "orders", type_="check")
    op.create_check_constraint(
        "ck_orders_status_valid",
        "orders",
        "status IN ('pending_payment', 'paid', 'cancelled')",
    )
    op.drop_column("orders", "expires_at")

