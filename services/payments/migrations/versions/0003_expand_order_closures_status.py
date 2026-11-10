"""expand payment_order_closures status with transient cancelling and expiring states

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Разширява payment_order_closures с временни статуси cancelling/expiring."""
    op.drop_constraint(
        "ck_payment_order_closures_status_valid",
        "payment_order_closures",
        type_="check",
    )
    op.create_check_constraint(
        "ck_payment_order_closures_status_valid",
        "payment_order_closures",
        "status IN ('cancelling', 'expiring', 'cancelled', 'expired')",
    )


def downgrade() -> None:
    """Връща constraint-а назад, ако няма активни cancelling/expiring записи."""
    conn = op.get_bind()
    transient_count = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM payment_order_closures "
            "WHERE status IN ('cancelling', 'expiring')"
        )
    ).scalar()
    if transient_count and transient_count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration 0003: found {transient_count} transient order closures"
        )

    op.drop_constraint(
        "ck_payment_order_closures_status_valid",
        "payment_order_closures",
        type_="check",
    )
    op.create_check_constraint(
        "ck_payment_order_closures_status_valid",
        "payment_order_closures",
        "status IN ('cancelled', 'expired')",
    )

