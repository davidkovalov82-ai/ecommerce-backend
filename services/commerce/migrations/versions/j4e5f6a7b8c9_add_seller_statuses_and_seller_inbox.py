"""add_seller_statuses_and_seller_inbox

Revision ID: j4e5f6a7b8c9
Revises: i3d4e5f6a7b8
Create Date: 2026-11-10 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "j4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "i3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Създава таблици seller_statuses и seller_inbox_events за автономна видимост на продавачите в каталога."""
    op.create_table(
        "seller_statuses",
        sa.Column("seller_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "seller_inbox_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("seller_id", sa.Integer(), nullable=False, index=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Начално запълване за съществуващи/пренесени продавачи от старата таблица users и съществуващите продукти
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO seller_statuses (seller_id, status, updated_at)
            SELECT id, status, now() FROM users
            ON CONFLICT (seller_id) DO NOTHING;
            """
        )
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO seller_statuses (seller_id, status, updated_at)
            SELECT DISTINCT seller_id, 'active', now() FROM products
            WHERE seller_id IS NOT NULL
            ON CONFLICT (seller_id) DO NOTHING;
            """
        )
    )


def downgrade() -> None:
    """Премахва seller_inbox_events и seller_statuses."""
    op.drop_table("seller_inbox_events")
    op.drop_table("seller_statuses")

