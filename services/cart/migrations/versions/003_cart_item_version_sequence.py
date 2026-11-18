"""add cart_item_version_seq sequence for monotonic versioning

Revision ID: 003_cart_version_seq
Revises: 002_cart_version_cleared
Create Date: 2026-11-18 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003_cart_version_seq"
down_revision: str | Sequence[str] | None = "002_cart_version_cleared"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Създава последователност cart_item_version_seq за гарантиране на уникални нарастващи версии."""
    op.execute(
        sa.text("CREATE SEQUENCE IF NOT EXISTS cart_item_version_seq START WITH 100;")
    )
    op.alter_column(
        "cart_items",
        "version",
        server_default=sa.text("nextval('cart_item_version_seq')"),
    )


def downgrade() -> None:
    """Връща промените."""
    op.alter_column("cart_items", "version", server_default="1")
    op.execute(sa.text("DROP SEQUENCE IF EXISTS cart_item_version_seq;"))
