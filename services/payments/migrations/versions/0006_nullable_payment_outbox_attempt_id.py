"""make payment_outbox_events.payment_attempt_id nullable for closure events

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Разрешава payment_attempt_id = NULL за поръчкови събития без платежна попытка."""
    op.alter_column(
        "payment_outbox_events",
        "payment_attempt_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    op.create_index(
        "ux_payment_outbox_order_event_no_attempt",
        "payment_outbox_events",
        ["order_id", "event_type"],
        unique=True,
        postgresql_where=sa.text("payment_attempt_id IS NULL"),
    )


def downgrade() -> None:
    """Връща NOT NULL, ако няма outbox записи без payment_attempt_id."""
    conn = op.get_bind()
    null_count = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM payment_outbox_events WHERE payment_attempt_id IS NULL"
        )
    ).scalar()
    if null_count and null_count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration 0006: found {null_count} outbox events without payment_attempt_id"
        )

    op.drop_index(
        "ux_payment_outbox_order_event_no_attempt",
        table_name="payment_outbox_events",
    )
    op.alter_column(
        "payment_outbox_events",
        "payment_attempt_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )

