"""add_seller_id_to_products

Revision ID: 9e7a669efa79
Revises: bd4c339e0dfa
Create Date: 2026-11-08 09:48:36.627050

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e7a669efa79"
down_revision: str | Sequence[str] | None = "bd4c339e0dfa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавя seller_id към products със съхраняване на съществуващите продукти."""
    # 1. Добавяме колона seller_id като nullable=True
    op.add_column("products", sa.Column("seller_id", sa.Integer(), nullable=True))

    # 2. Намираме или създаваме технически акаунт за историческите продукти
    users_table = sa.table(
        "users",
        sa.column("id", sa.Integer),
        sa.column("email", sa.String),
        sa.column("password_hash", sa.String),
        sa.column("role", sa.String),
    )
    conn = op.get_bind()
    tech_user_id = conn.execute(
        sa.select(users_table.c.id).where(
            users_table.c.email == "legacy-catalog@internal.ecommerce.com"
        )
    ).scalar()

    if tech_user_id is None:
        tech_user_id = conn.execute(
            sa.insert(users_table)
            .values(
                email="legacy-catalog@internal.ecommerce.com",
                password_hash="!disabled",
                role="customer",
            )
            .returning(users_table.c.id)
        ).scalar()

    # 3. Запълваме seller_id за всички съществуващи продукти, които нямат такъв
    products_table = sa.table(
        "products",
        sa.column("id", sa.Integer),
        sa.column("seller_id", sa.Integer),
    )
    conn.execute(
        products_table.update()
        .where(products_table.c.seller_id.is_(None))
        .values(seller_id=tech_user_id)
    )

    # 4. Установяваме NOT NULL върху products.seller_id
    op.alter_column("products", "seller_id", nullable=False)

    # 5. Създаваме Foreign Key и индекс
    op.create_foreign_key(
        "fk_products_seller_id_users",
        "products",
        "users",
        ["seller_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_products_seller_id", "products", ["seller_id"])


def downgrade() -> None:
    """Връща миграцията назад."""
    op.drop_index("ix_products_seller_id", table_name="products")
    op.drop_constraint("fk_products_seller_id_users", "products", type_="foreignkey")
    op.drop_column("products", "seller_id")

    users_table = sa.table(
        "users",
        sa.column("id", sa.Integer),
        sa.column("email", sa.String),
    )
    conn = op.get_bind()
    conn.execute(
        users_table.delete().where(
            users_table.c.email == "legacy-catalog@internal.ecommerce.com"
        )
    )
