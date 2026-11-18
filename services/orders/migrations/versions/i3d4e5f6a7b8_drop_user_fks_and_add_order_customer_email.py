"""drop foreign keys to users.id and add customer_email to orders

Revision ID: i3d4e5f6a7b8
Revises: h2c3d4e5f6a7
Create Date: 2026-11-10 19:30:00.000000

"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "i3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "h2c3d4e5f6a7"
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
    """Премахва блокиращите външни ключове към users.id и добавя customer_email към orders."""
    # 1. Сваляме външните ключове към таблицата users
    _drop_fk_if_exists("products", "fk_products_seller_id_users")
    _drop_fk_if_exists("cart_items", "fk_cart_items_user_id_users")
    _drop_fk_if_exists("orders", "orders_user_id_fkey")
    _drop_fk_if_exists("order_items", "order_items_seller_id_fkey")
    _drop_fk_if_exists("shipments", "shipments_seller_id_fkey")

    # 2. Добавяме customer_email колона към orders
    op.add_column(
        "orders",
        sa.Column("customer_email", sa.String(length=255), nullable=True),
    )

    # 3. Попълваме customer_email от историческата таблица users за съществуващите поръчки
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE orders "
            "SET customer_email = users.email "
            "FROM users "
            "WHERE orders.user_id = users.id AND orders.customer_email IS NULL"
        )
    )


def _create_fk_if_tables_exist(
    constraint_name: str,
    source_table: str,
    referent_table: str,
    local_cols: list[str],
    remote_cols: list[str],
    **kwargs: Any,
) -> None:
    conn = op.get_bind()
    src_exists = conn.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
        {"t": source_table},
    ).scalar()
    ref_exists = conn.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
        {"t": referent_table},
    ).scalar()
    if src_exists and ref_exists:
        op.create_foreign_key(
            constraint_name,
            source_table,
            referent_table,
            local_cols,
            remote_cols,
            **kwargs,
        )


def downgrade() -> None:
    """Възстановява външните ключове и премахва customer_email."""
    op.drop_column("orders", "customer_email")

    _create_fk_if_tables_exist(
        "fk_products_seller_id_users",
        "products",
        "users",
        ["seller_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _create_fk_if_tables_exist(
        "fk_cart_items_user_id_users",
        "cart_items",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    _create_fk_if_tables_exist(
        "orders_user_id_fkey",
        "orders",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _create_fk_if_tables_exist(
        "order_items_seller_id_fkey",
        "order_items",
        "users",
        ["seller_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _create_fk_if_tables_exist(
        "shipments_seller_id_fkey",
        "shipments",
        "users",
        ["seller_id"],
        ["id"],
        ondelete="RESTRICT",
    )
