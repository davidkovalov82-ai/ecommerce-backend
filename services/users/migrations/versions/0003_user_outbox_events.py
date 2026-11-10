"""create user outbox events table

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10 20:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Създава таблица user_outbox_events за надеждно публикуване на събития за статус на потребители."""
    op.create_table(
        "user_outbox_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_user_outbox_events_event_id"),
    )
    op.create_index(
        "ix_user_outbox_events_event_type",
        "user_outbox_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        "ix_user_outbox_events_user_id",
        "user_outbox_events",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_outbox_events_published_at",
        "user_outbox_events",
        ["published_at"],
        unique=False,
    )


def downgrade() -> None:
    """Премахва таблица user_outbox_events."""
    op.drop_index("ix_user_outbox_events_published_at", table_name="user_outbox_events")
    op.drop_index("ix_user_outbox_events_user_id", table_name="user_outbox_events")
    op.drop_index("ix_user_outbox_events_event_type", table_name="user_outbox_events")
    op.drop_table("user_outbox_events")

