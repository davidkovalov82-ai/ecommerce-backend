"""add version to cart_items and cart_cleared_orders table

Revision ID: 002_cart_version_cleared
Revises: 001_cart_initial
Create Date: 2026-11-17 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_cart_version_cleared"
down_revision: str | Sequence[str] | None = "001_cart_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавя колона version в cart_items и създава таблица cart_cleared_orders."""
    op.add_column(
        "cart_items",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_table(
        "cart_cleared_orders",
        sa.Column("order_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "cleared_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Връща промените."""
    op.drop_table("cart_cleared_orders")
    op.drop_column("cart_items", "version")
