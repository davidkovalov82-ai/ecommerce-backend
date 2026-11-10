"""extract payments service inbox discrepancy and attempt tracking

Revision ID: g1b2c3d4e5f6
Revises: f7a8b9c0d1e2
Create Date: 2026-11-10 10:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Подготвя Commerce за събитийната интеграция с Payments без триене на старите таблици."""
    op.add_column(
        "orders",
        sa.Column(
            "payment_attempt_seq",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "active_attempt_key",
            sa.String(length=100),
            nullable=True,
        ),
    )

    # Попълваме active_attempt_key от последната stripe_checkout_sessions (ако има), иначе order_<id>_attempt_1
    op.execute(
        sa.text(
            """
            UPDATE orders o
            SET active_attempt_key = COALESCE(
                (
                    SELECT s.idempotency_key
                    FROM stripe_checkout_sessions s
                    WHERE s.order_id = o.id
                    ORDER BY s.id DESC
                    LIMIT 1
                ),
                'order_' || o.id::text || '_attempt_1'
            )
            """
        )
    )

    op.create_table(
        "order_inbox_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("payment_attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_order_inbox_events_order_id",
        "order_inbox_events",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_inbox_events_attempt_id",
        "order_inbox_events",
        ["payment_attempt_id"],
        unique=False,
    )

    op.create_table(
        "order_payment_discrepancies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("payment_attempt_id", sa.BigInteger(), nullable=True),
        sa.Column("event_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("stripe_session_id", sa.String(length=255), nullable=True),
        sa.Column("stripe_event_id", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="open",
            nullable=False,
        ),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_order_payment_discrepancies_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            name="uq_order_payment_discrepancies_event_id",
        ),
    )
    op.create_index(
        "ix_order_payment_discrepancies_order_id",
        "order_payment_discrepancies",
        ["order_id"],
        unique=False,
    )


def downgrade() -> None:
    """Премахва К5 таблиците, само ако няма записани платежни разхождения."""
    conn = op.get_bind()
    open_discrepancies = conn.execute(
        sa.text("SELECT COUNT(*) FROM order_payment_discrepancies")
    ).scalar_one()
    if open_discrepancies > 0:
        raise RuntimeError(
            f"Cannot downgrade migration g1b2c3d4e5f6: found {open_discrepancies} payment discrepancies"
        )

    op.drop_index(
        "ix_order_payment_discrepancies_order_id",
        table_name="order_payment_discrepancies",
    )
    op.drop_table("order_payment_discrepancies")
    op.drop_index("ix_order_inbox_events_attempt_id", table_name="order_inbox_events")
    op.drop_index("ix_order_inbox_events_order_id", table_name="order_inbox_events")
    op.drop_table("order_inbox_events")
    op.drop_column("orders", "active_attempt_key")
    op.drop_column("orders", "payment_attempt_seq")
