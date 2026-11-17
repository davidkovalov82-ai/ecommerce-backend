"""initial cart schema

Revision ID: 001_cart_initial
Revises:
Create Date: 2026-11-17 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001_cart_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Създава изолираната таблица cart_items в Cart услугата."""
    op.create_table(
        "cart_items",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("user_id", "product_id", name="pk_cart_items"),
        sa.CheckConstraint(
            "quantity >= 1 AND quantity <= 99",
            name="ck_cart_items_quantity_range",
        ),
    )


def downgrade() -> None:
    """Премахва таблицата cart_items."""
    op.drop_table("cart_items")
