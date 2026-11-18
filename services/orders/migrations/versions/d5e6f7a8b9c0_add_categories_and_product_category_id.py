"""add_categories_and_product_category_id

Revision ID: d5e6f7a8b9c0
Revises: f3c8a91b4d2e
Create Date: 2026-11-08 11:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "f3c8a91b4d2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Създава таблица categories и добавя products.category_id."""
    # 1. Таблица categories
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
    )
    # Уникален функционален индекс по lower(trim(name)) за предотвратяване на дублирани имена
    op.create_index(
        "uq_categories_name_lower",
        "categories",
        [sa.text("lower(trim(name))")],
        unique=True,
    )

    # 2. Добавяне на category_id към products
    op.add_column(
        "products",
        sa.Column(
            "category_id",
            sa.Integer(),
            sa.ForeignKey("categories.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_products_category_id",
        "products",
        ["category_id"],
    )


def downgrade() -> None:
    """Премахва products.category_id и таблицата categories."""
    op.drop_index("ix_products_category_id", table_name="products")
    op.drop_column("products", "category_id")
    op.drop_index("uq_categories_name_lower", table_name="categories")
    op.drop_table("categories")
