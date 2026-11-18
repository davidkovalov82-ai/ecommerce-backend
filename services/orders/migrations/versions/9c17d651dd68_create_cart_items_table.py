"""create_cart_items_table

Revision ID: 9c17d651dd68
Revises: 4af78c666cf2
Create Date: 2026-11-05 20:30:18.668183

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c17d651dd68"
down_revision: str | Sequence[str] | None = "4af78c666cf2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: създаване на таблица cart_items с ограничения и външни ключове."""
    op.create_table(
        "cart_items",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "quantity >= 1 AND quantity <= 99",
            name="ck_cart_items_quantity_range",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_cart_items_product_id_products",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_cart_items_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "product_id", name="pk_cart_items"),
    )


def downgrade() -> None:
    """Downgrade schema: изтриване на таблица cart_items."""
    op.drop_table("cart_items")
