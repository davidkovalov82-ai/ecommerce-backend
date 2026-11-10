"""add order closures and payment discrepancies tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавя payment_order_closures и payment_discrepancies в Payments БД."""
    op.create_table(
        "payment_order_closures",
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=50), nullable=False),
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('cancelled', 'expired')",
            name="ck_payment_order_closures_status_valid",
        ),
        sa.PrimaryKeyConstraint("order_id"),
    )

    op.create_table(
        "payment_discrepancies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("payment_attempt_id", sa.BigInteger(), nullable=True),
        sa.Column("stripe_session_id", sa.String(length=255), nullable=True),
        sa.Column("stripe_event_id", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("expected_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("actual_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("expected_currency", sa.String(length=10), nullable=True),
        sa.Column("actual_currency", sa.String(length=10), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('webhook', 'reconcile', 'cancel_conflict')",
            name="ck_payment_discrepancies_source_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_payment_discrepancies_order_id",
        "payment_discrepancies",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        "ix_payment_discrepancies_attempt_id",
        "payment_discrepancies",
        ["payment_attempt_id"],
        unique=False,
    )


def downgrade() -> None:
    """Отказва downgrade, ако има диагностични разхождения, и иначе премахва новите таблици."""
    conn = op.get_bind()
    discrepancy_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM payment_discrepancies")
    ).scalar()
    if discrepancy_count and discrepancy_count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration 0002: found {discrepancy_count} payment discrepancies"
        )

    op.drop_index(
        "ix_payment_discrepancies_attempt_id", table_name="payment_discrepancies"
    )
    op.drop_index(
        "ix_payment_discrepancies_order_id", table_name="payment_discrepancies"
    )
    op.drop_table("payment_discrepancies")
    op.drop_table("payment_order_closures")
