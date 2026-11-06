"""create_stripe_tables

Revision ID: e2b83a741f90
Revises: c74a9d821e5f
Create Date: 2026-11-06 09:35:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b83a741f90"
down_revision: str | Sequence[str] | None = "c74a9d821e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: създаване на таблици stripe_checkout_sessions и stripe_processed_events."""
    op.create_table(
        "stripe_checkout_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=50),
            nullable=False,
            server_default="initiated",
        ),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default="EUR",
        ),
        sa.Column("stripe_session_id", sa.String(length=255), nullable=True),
        sa.Column("checkout_url", sa.String(length=1024), nullable=True),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "stripe_session_id",
            name="uq_stripe_checkout_sessions_session_id",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_stripe_checkout_sessions_idempotency_key",
        ),
        sa.CheckConstraint(
            "status IN ('initiated', 'open', 'completed', 'cancelling', 'expired', 'failed')",
            name="ck_stripe_checkout_sessions_status_valid",
        ),
        sa.CheckConstraint(
            "amount_minor >= 0",
            name="ck_stripe_checkout_sessions_amount_minor_non_negative",
        ),
        sa.CheckConstraint(
            "currency = 'EUR'",
            name="ck_stripe_checkout_sessions_currency_eur",
        ),
    )
    op.create_index(
        "ix_stripe_checkout_sessions_order_id",
        "stripe_checkout_sessions",
        ["order_id"],
    )

    op.create_table(
        "stripe_processed_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("stripe_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "stripe_event_id",
            name="uq_stripe_processed_events_event_id",
        ),
    )
    op.create_index(
        "ix_stripe_processed_events_order_id",
        "stripe_processed_events",
        ["order_id"],
    )


def downgrade() -> None:
    """Downgrade schema: изтриване на stripe_processed_events и stripe_checkout_sessions."""
    op.drop_index(
        "ix_stripe_processed_events_order_id",
        table_name="stripe_processed_events",
    )
    op.drop_table("stripe_processed_events")
    op.drop_index(
        "ix_stripe_checkout_sessions_order_id",
        table_name="stripe_checkout_sessions",
    )
    op.drop_table("stripe_checkout_sessions")
