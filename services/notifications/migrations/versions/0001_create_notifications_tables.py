"""create_notifications_tables

Revision ID: 0001_notifications_init
Revises:
Create Date: 2026-11-10 08:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_notifications_init"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Създава таблиците inbox_events и notification_deliveries с ограничения за уникалност."""
    op.create_table(
        "inbox_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("shipment_id", sa.BigInteger(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_inbox_events"),
    )
    op.create_index("ix_inbox_events_order_id", "inbox_events", ["order_id"])
    op.create_index("ix_inbox_events_shipment_id", "inbox_events", ["shipment_id"])
    op.create_index("ix_inbox_events_received_at", "inbox_events", ["received_at"])

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column(
            "next_retry_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_token", sa.String(length=36), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('email', 'sms')",
            name="ck_notification_deliveries_channel_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed', 'skipped')",
            name="ck_notification_deliveries_status_valid",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_notification_deliveries_attempt_non_negative",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="ck_notification_deliveries_max_attempts_positive",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["inbox_events.event_id"],
            name="fk_notification_deliveries_event_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_deliveries"),
        sa.UniqueConstraint(
            "event_id",
            "channel",
            name="uq_notification_deliveries_event_channel",
        ),
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "ix_notification_deliveries_locked_until",
        "notification_deliveries",
        ["locked_until"],
    )
    op.create_index(
        "ix_notification_deliveries_event_id",
        "notification_deliveries",
        ["event_id"],
    )


def downgrade() -> None:
    """Премахва таблиците notification_deliveries и inbox_events."""
    op.drop_index(
        "ix_notification_deliveries_event_id",
        table_name="notification_deliveries",
    )
    op.drop_index(
        "ix_notification_deliveries_locked_until",
        table_name="notification_deliveries",
    )
    op.drop_index(
        "ix_notification_deliveries_due",
        table_name="notification_deliveries",
    )
    op.drop_table("notification_deliveries")

    op.drop_index("ix_inbox_events_received_at", table_name="inbox_events")
    op.drop_index("ix_inbox_events_shipment_id", table_name="inbox_events")
    op.drop_index("ix_inbox_events_order_id", table_name="inbox_events")
    op.drop_table("inbox_events")
