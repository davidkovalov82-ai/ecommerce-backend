"""remove legacy domain tables for orders service isolation

Revision ID: l6a7b8c9d0e1
Revises: k5f6a7b8c9d0
Create Date: 2026-11-17 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "l6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "k5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Премахва остатъчните таблици от други домейни (Users, Catalog, Cart)."""
    conn = op.get_bind()
    tables_to_drop = [
        "cart_items",
        "seller_inbox_events",
        "seller_statuses",
        "categories",
        "products",
        "users",
    ]
    for table_name in tables_to_drop:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {table_name} CASCADE;"))


def downgrade() -> None:
    pass
