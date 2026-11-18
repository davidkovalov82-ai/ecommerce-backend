"""create_payment_attempts_and_expand_orders_status

Revision ID: c74a9d821e5f
Revises: 568b759ac3a0
Create Date: 2026-11-06 09:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c74a9d821e5f"
down_revision: str | Sequence[str] | None = "568b759ac3a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: разширяване на статуса на orders и създаване на payment_attempts."""
    # 1. Разширяване на CHECK ограничението за статус в orders
    op.drop_constraint("ck_orders_status_valid", "orders", type_="check")
    op.create_check_constraint(
        "ck_orders_status_valid",
        "orders",
        "status IN ('pending_payment', 'paid', 'cancelled')",
    )

    # 2. Създаване на таблица payment_attempts
    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payment_method", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default="EUR",
        ),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "order_id",
            "idempotency_key",
            name="uq_payment_attempts_order_id_idempotency_key",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'declined')",
            name="ck_payment_attempts_status_valid",
        ),
        sa.CheckConstraint(
            "amount_minor >= 0",
            name="ck_payment_attempts_amount_minor_non_negative",
        ),
        sa.CheckConstraint("currency = 'EUR'", name="ck_payment_attempts_currency_eur"),
    )
    op.create_index(
        "ix_payment_attempts_order_id",
        "payment_attempts",
        ["order_id"],
    )
    op.create_index(
        "uq_payment_attempts_one_succeeded_per_order",
        "payment_attempts",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
    )


def downgrade() -> None:
    """Downgrade schema: изтриване на payment_attempts и възстановяване на ck_orders_status_valid."""
    op.drop_index(
        "uq_payment_attempts_one_succeeded_per_order",
        table_name="payment_attempts",
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    op.drop_index("ix_payment_attempts_order_id", table_name="payment_attempts")
    op.drop_table("payment_attempts")

    op.drop_constraint("ck_orders_status_valid", "orders", type_="check")
    op.create_check_constraint(
        "ck_orders_status_valid",
        "orders",
        "status = 'pending_payment'",
    )
