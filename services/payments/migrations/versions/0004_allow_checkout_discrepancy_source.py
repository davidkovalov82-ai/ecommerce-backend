"""allow checkout source in payment_discrepancies

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-10 12:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Разширява payment_discrepancies.source с checkout."""
    op.drop_constraint(
        "ck_payment_discrepancies_source_valid",
        "payment_discrepancies",
        type_="check",
    )
    op.create_check_constraint(
        "ck_payment_discrepancies_source_valid",
        "payment_discrepancies",
        "source IN ('webhook', 'reconcile', 'cancel_conflict', 'checkout')",
    )


def downgrade() -> None:
    """Връща constraint-а назад, ако няма записи със source='checkout'."""
    conn = op.get_bind()
    checkout_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM payment_discrepancies WHERE source = 'checkout'")
    ).scalar()
    if checkout_count and checkout_count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration 0004: found {checkout_count} checkout discrepancies"
        )

    op.drop_constraint(
        "ck_payment_discrepancies_source_valid",
        "payment_discrepancies",
        type_="check",
    )
    op.create_check_constraint(
        "ck_payment_discrepancies_source_valid",
        "payment_discrepancies",
        "source IN ('webhook', 'reconcile', 'cancel_conflict')",
    )
