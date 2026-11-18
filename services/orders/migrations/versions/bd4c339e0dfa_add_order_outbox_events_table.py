"""add_order_outbox_events_table

Revision ID: bd4c339e0dfa
Revises: b4f8c2e1a9d0
Create Date: 2026-11-06 17:44:38.169439

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bd4c339e0dfa"
down_revision: str | Sequence[str] | None = "b4f8c2e1a9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Създава таблица order_outbox_events за надеждно изпращане на събития по модела Transactional Outbox."""
    op.create_table(
        "order_outbox_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "event_type",
            sa.String(length=50),
            nullable=False,
            server_default="order.paid",
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_outbox_events_order_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_outbox_events"),
        sa.UniqueConstraint(
            "order_id",
            "event_type",
            name="uq_order_outbox_order_id_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name="ck_order_outbox_status_valid",
        ),
    )
    op.create_index(
        "ix_order_outbox_order_id",
        "order_outbox_events",
        ["order_id"],
    )
    op.create_index(
        "ix_order_outbox_status_created",
        "order_outbox_events",
        ["status", "created_at"],
    )


def downgrade() -> None:
    """Премахва таблица order_outbox_events."""
    op.drop_index("ix_order_outbox_status_created", table_name="order_outbox_events")
    op.drop_index("ix_order_outbox_order_id", table_name="order_outbox_events")
    op.drop_table("order_outbox_events")
