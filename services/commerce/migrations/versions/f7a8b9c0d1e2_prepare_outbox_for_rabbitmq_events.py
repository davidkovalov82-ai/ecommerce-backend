"""prepare_outbox_for_rabbitmq_events

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-11-10 08:30:00.000000

Migration policy:
- Preserves historical outbox semantics: rows already delivered via the legacy
  Commerce SMTP outbox worker (`status = 'sent'`) are converted to `'legacy_sent'`
  so they are NEVER republished or emailed again after cutover.
- Unsent rows (`status = 'pending'`) stay `'pending'` so the RabbitMQ outbox
  publisher delivers them to the Notifications service.
- Assigns stable `event_id` (UUID), `event_version = 1`, `occurred_at = created_at`,
  and adds `published_at`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Разширява order_outbox_events за RabbitMQ събития без повторно пращане на историческите писма."""
    op.add_column(
        "order_outbox_events",
        sa.Column("event_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "order_outbox_events",
        sa.Column(
            "event_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "order_outbox_events",
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "order_outbox_events",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        sa.text(
            "UPDATE order_outbox_events "
            "SET event_id = gen_random_uuid()::text "
            "WHERE event_id IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE order_outbox_events "
            "SET occurred_at = COALESCE(occurred_at, created_at, now()) "
            "WHERE occurred_at IS NULL"
        )
    )

    op.drop_constraint(
        "ck_order_outbox_status_valid",
        "order_outbox_events",
        type_="check",
    )
    op.execute(
        sa.text(
            "UPDATE order_outbox_events "
            "SET status = 'legacy_sent' "
            "WHERE status = 'sent'"
        )
    )
    op.create_check_constraint(
        "ck_order_outbox_status_valid",
        "order_outbox_events",
        "status IN ('pending', 'legacy_sent', 'published', 'failed')",
    )

    op.alter_column(
        "order_outbox_events",
        "event_id",
        existing_type=sa.String(length=36),
        nullable=False,
        server_default=sa.text("gen_random_uuid()::text"),
    )
    op.alter_column(
        "order_outbox_events",
        "occurred_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    op.create_unique_constraint(
        "uq_order_outbox_event_id",
        "order_outbox_events",
        ["event_id"],
    )


def downgrade() -> None:
    """Връща схемата на order_outbox_events, запазвайки изпратените и публикувани събития като 'sent'."""
    op.drop_constraint(
        "uq_order_outbox_event_id",
        "order_outbox_events",
        type_="unique",
    )
    op.drop_constraint(
        "ck_order_outbox_status_valid",
        "order_outbox_events",
        type_="check",
    )
    op.execute(
        sa.text(
            "UPDATE order_outbox_events "
            "SET status = 'sent', "
            "    processed_at = COALESCE(processed_at, published_at, now()) "
            "WHERE status IN ('legacy_sent', 'published')"
        )
    )
    op.create_check_constraint(
        "ck_order_outbox_status_valid",
        "order_outbox_events",
        "status IN ('pending', 'sent', 'failed')",
    )
    op.drop_column("order_outbox_events", "published_at")
    op.drop_column("order_outbox_events", "occurred_at")
    op.drop_column("order_outbox_events", "event_version")
    op.drop_column("order_outbox_events", "event_id")
