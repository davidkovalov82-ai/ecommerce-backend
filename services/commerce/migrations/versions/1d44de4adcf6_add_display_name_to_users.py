"""add_display_name_to_users

Revision ID: 1d44de4adcf6
Revises: 9e7a669efa79
Create Date: 2026-11-08 10:28:17.528172

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1d44de4adcf6"
down_revision: str | Sequence[str] | None = "9e7a669efa79"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавя незадължително поле display_name към таблица users."""
    op.add_column(
        "users", sa.Column("display_name", sa.String(length=80), nullable=True)
    )


def downgrade() -> None:
    """Премахва полето display_name от таблица users."""
    op.drop_column("users", "display_name")
