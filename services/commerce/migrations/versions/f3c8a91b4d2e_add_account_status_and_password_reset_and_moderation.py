"""add_account_status_and_password_reset_and_moderation

Revision ID: f3c8a91b4d2e
Revises: 09ee8360d8c2
Create Date: 2026-11-08 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3c8a91b4d2e"
down_revision: str | Sequence[str] | None = "09ee8360d8c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавя статус на потребителя, таблица за токени за възстановяване на парола и одит лог за модерация."""
    # 1. Колона status в users със стойност по подразбиране 'active'
    op.add_column(
        "users",
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
    )

    # 2. Таблица password_reset_tokens за хеширани еднократни токени
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "token_hash",
            sa.String(64),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # 3. Таблица account_moderation_logs за журналиране на модераторски действия
    op.create_table(
        "account_moderation_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "admin_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    """Премахва таблиците за одит и токени и колоната status."""
    op.drop_table("account_moderation_logs")
    op.drop_table("password_reset_tokens")
    op.drop_column("users", "status")

