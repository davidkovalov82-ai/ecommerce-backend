"""prepare_outbox_for_rabbitmq_events

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-11-10 08:30:00.000000

Migration & Downgrade policy:
- Upgrade preserves historical outbox semantics: rows already delivered via the
  legacy Commerce SMTP outbox worker (`status = 'sent'`) are converted to
  `'legacy_sent'` so they are NEVER republished or emailed again after cutover.
  Unsent rows (`status = 'pending'`) stay `'pending'` for the RabbitMQ publisher.
- Downgrade is explicitly blocked before any schema change if any row currently
  has `status = 'published'`: Commerce cannot prove from its own database
  whether those RabbitMQ-published events have already been delivered by the
  independent Notifications service. Converting `'published'` silently to `'sent'`
  or `'pending'` would either hide undelivered notifications or risk duplicates.
  If no `'published'` rows exist, downgrade safely converts `'legacy_sent'` back
  to `'sent'` (preserving original `processed_at`) and removes the K4 columns.
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
    """Връща схемата безопасно, като отказва откат при наличие на 'published' събития."""
    bind = op.get_bind()
    published_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM order_outbox_events WHERE status = 'published'")
    ).scalar()
    if published_count and published_count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration {revision}: found {published_count} 'published' "
            "events in order_outbox_events whose delivery by the independent Notifications "
            "service cannot be proven from the Commerce database. "
            "Downgrade is blocked before schema changes to protect delivery guarantees."
        )

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
            "SET status = 'sent' "
            "WHERE status = 'legacy_sent'"
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
