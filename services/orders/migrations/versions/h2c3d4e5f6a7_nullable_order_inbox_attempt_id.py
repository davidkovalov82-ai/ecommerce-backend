"""make order_inbox_events.payment_attempt_id nullable for closure events

Revision ID: h2c3d4e5f6a7
Revises: g1b2c3d4e5f6
Create Date: 2026-11-10 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "g1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Разрешава payment_attempt_id = NULL за поръчкови събития без платежна попытка."""
    op.alter_column(
        "order_inbox_events",
        "payment_attempt_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade() -> None:
    """Връща NOT NULL, ако няма inbox записи без payment_attempt_id."""
    conn = op.get_bind()
    null_count = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM order_inbox_events WHERE payment_attempt_id IS NULL"
        )
    ).scalar()
    if null_count and null_count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration h2c3d4e5f6a7: found {null_count} inbox events without payment_attempt_id"
        )

    op.alter_column(
        "order_inbox_events",
        "payment_attempt_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
