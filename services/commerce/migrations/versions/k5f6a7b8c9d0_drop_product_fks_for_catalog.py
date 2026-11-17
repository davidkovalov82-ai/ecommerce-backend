"""drop product foreign keys for catalog service extraction

Revision ID: k5f6a7b8c9d0
Revises: j4e5f6a7b8c9
Create Date: 2026-11-17 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "j4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_fk_if_exists(table_name: str, constraint_name: str) -> None:
    conn = op.get_bind()
    exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_name = :cname AND table_name = :tname AND constraint_type = 'FOREIGN KEY'"
        ),
        {"cname": constraint_name, "tname": table_name},
    ).scalar()
    if exists:
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")


def upgrade() -> None:
    """Премахва външните ключове към таблицата products в cart_items и order_items."""
    _drop_fk_if_exists("cart_items", "cart_items_product_id_fkey")
    _drop_fk_if_exists("cart_items", "fk_cart_items_product_id_products")
    _drop_fk_if_exists("order_items", "order_items_product_id_fkey")
    _drop_fk_if_exists("order_items", "fk_order_items_product_id_products")


def downgrade() -> None:
    pass
