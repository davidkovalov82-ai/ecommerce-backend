"""sync_products_id_sequence

Revision ID: 4af78c666cf2
Revises: 5264e9a0fb31
Create Date: 2026-11-03 19:02:43.853173

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4af78c666cf2"
down_revision: str | Sequence[str] | None = "5264e9a0fb31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: синхронизира PostgreSQL sequence-а на products.id с актуалния MAX(id)."""
    op.execute(
        "SELECT setval("
        "pg_get_serial_sequence('products', 'id'), "
        "COALESCE(MAX(id), 1), "
        "MAX(id) IS NOT NULL"
        ") FROM products;"
    )


def downgrade() -> None:
    """Downgrade schema."""
