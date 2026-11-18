"""create_orders_and_order_items_tables

Revision ID: 568b759ac3a0
Revises: 9c17d651dd68
Create Date: 2026-11-05 21:25:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "568b759ac3a0"
down_revision: str | Sequence[str] | None = "9c17d651dd68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: създаване на таблици orders и order_items."""
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=50),
            nullable=False,
            server_default="pending_payment",
        ),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default="EUR",
        ),
        sa.Column("total_minor", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_orders_user_id_idempotency_key"
        ),
        sa.CheckConstraint("status = 'pending_payment'", name="ck_orders_status_valid"),
        sa.CheckConstraint("currency = 'EUR'", name="ck_orders_currency_eur"),
        sa.CheckConstraint(
            "total_minor >= 0", name="ck_orders_total_minor_non_negative"
        ),
    )
    op.create_index(
        "ix_orders_user_created_id",
        "orders",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )

    op.create_table(
        "order_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("product_title", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_minor", sa.Integer(), nullable=False),
        sa.Column("subtotal_minor", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "order_id", "product_id", name="uq_order_items_order_id_product_id"
        ),
        sa.CheckConstraint(
            "quantity >= 1 AND quantity <= 99",
            name="ck_order_items_quantity_range",
        ),
        sa.CheckConstraint(
            "unit_price_minor >= 0",
            name="ck_order_items_unit_price_minor_non_negative",
        ),
        sa.CheckConstraint(
            "subtotal_minor >= 0",
            name="ck_order_items_subtotal_minor_non_negative",
        ),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])


def downgrade() -> None:
    """Downgrade schema: изтриване на таблици order_items и orders."""
    op.drop_table("order_items")
    op.drop_table("orders")
