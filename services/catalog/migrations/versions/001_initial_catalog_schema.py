"""Initial catalog schema

Revision ID: 001_catalog_init
Revises:
Create Date: 2026-11-17 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001_catalog_init"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. categories
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_categories_name_lower",
        "categories",
        [sa.text("lower(trim(name))")],
        unique=True,
    )

    # 2. products
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("seller_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("price_minor", sa.Integer(), nullable=False),
        sa.Column(
            "currency", sa.String(length=3), server_default="EUR", nullable=False
        ),
        sa.Column("stock", sa.Integer(), server_default="0", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.CheckConstraint(
            "price_minor >= 0", name="ck_products_price_minor_non_negative"
        ),
        sa.CheckConstraint("stock >= 0", name="ck_products_stock_non_negative"),
        sa.CheckConstraint("currency = 'EUR'", name="ck_products_currency_eur"),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_products_seller_id"), "products", ["seller_id"], unique=False
    )
    op.create_index(
        op.f("ix_products_category_id"), "products", ["category_id"], unique=False
    )

    # 3. seller_statuses
    op.create_table(
        "seller_statuses",
        sa.Column("seller_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="active", nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("seller_id"),
    )

    # 4. seller_inbox_events
    op.create_table(
        "seller_inbox_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("seller_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        op.f("ix_seller_inbox_events_seller_id"),
        "seller_inbox_events",
        ["seller_id"],
        unique=False,
    )

    # 5. stock_reservations
    op.create_table(
        "stock_reservations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="reserved", nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_stock_reservations_qty_positive"),
        sa.CheckConstraint(
            "status IN ('reserved', 'released')",
            name="ck_stock_reservations_status_valid",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "order_id", "product_id", name="uq_stock_reservations_order_id_product_id"
        ),
    )
    op.create_index(
        op.f("ix_stock_reservations_order_id"),
        "stock_reservations",
        ["order_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("stock_reservations")
    op.drop_table("seller_inbox_events")
    op.drop_table("seller_statuses")
    op.drop_table("products")
    op.drop_table("categories")
