"""add_token_version_to_users

Revision ID: 09ee8360d8c2
Revises: 1d44de4adcf6
Create Date: 2026-11-08 10:37:49.123456

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "09ee8360d8c2"
down_revision: str | Sequence[str] | None = "1d44de4adcf6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавя колона token_version към таблица users със стойност по подразбиране 0."""
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    """Премахва колона token_version от таблица users."""
    op.drop_column("users", "token_version")
