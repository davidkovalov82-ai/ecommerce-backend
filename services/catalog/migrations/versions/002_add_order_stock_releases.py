"""add order_stock_releases table for terminal release tracking

Revision ID: 002_catalog_order_releases
Revises: 001_catalog_init
Create Date: 2026-11-18 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_catalog_order_releases"
down_revision: str | Sequence[str] | None = "001_catalog_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Създава таблица order_stock_releases за терминално проследяване на освободени поръчки."""
    op.create_table(
        "order_stock_releases",
        sa.Column("order_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "released_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("order_id"),
    )


def downgrade() -> None:
    """Връща промените."""
    op.drop_table("order_stock_releases")
