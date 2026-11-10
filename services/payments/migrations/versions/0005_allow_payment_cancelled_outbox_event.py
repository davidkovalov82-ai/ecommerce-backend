"""allow payment.cancelled in payment_outbox_events

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Разширява payment_outbox_events.event_type с payment.cancelled."""
    op.drop_constraint(
        "ck_payment_outbox_event_type_valid",
        "payment_outbox_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_payment_outbox_event_type_valid",
        "payment_outbox_events",
        "event_type IN ("
        "'payment.succeeded', "
        "'payment.processing', "
        "'payment.failed', "
        "'payment.expired', "
        "'payment.cancelled'"
        ")",
    )


def downgrade() -> None:
    """Връща constraint-а назад, ако няма записи с event_type='payment.cancelled'."""
    conn = op.get_bind()
    cancelled_count = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM payment_outbox_events "
            "WHERE event_type = 'payment.cancelled'"
        )
    ).scalar()
    if cancelled_count and cancelled_count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration 0005: found {cancelled_count} payment.cancelled outbox events"
        )

    op.drop_constraint(
        "ck_payment_outbox_event_type_valid",
        "payment_outbox_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_payment_outbox_event_type_valid",
        "payment_outbox_events",
        "event_type IN ('payment.succeeded', 'payment.processing', 'payment.failed', 'payment.expired')",
    )
