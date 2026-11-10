"""create password reset and moderation tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10 13:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Създава таблици за еднократни токени за възстановяване на парола и одит лог за модерация."""
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_token_hash"),
    )
    op.create_index(
        "ix_password_reset_tokens_user_id",
        "password_reset_tokens",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_password_reset_tokens_token_hash",
        "password_reset_tokens",
        ["token_hash"],
        unique=True,
    )

    op.create_table(
        "account_moderation_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["admin_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_account_moderation_logs_user_id",
        "account_moderation_logs",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_account_moderation_logs_admin_id",
        "account_moderation_logs",
        ["admin_id"],
        unique=False,
    )


def downgrade() -> None:
    """Премахва таблиците за модерация и възстановяване на парола."""
    op.drop_index(
        "ix_account_moderation_logs_admin_id", table_name="account_moderation_logs"
    )
    op.drop_index(
        "ix_account_moderation_logs_user_id", table_name="account_moderation_logs"
    )
    op.drop_table("account_moderation_logs")

    op.drop_index(
        "ix_password_reset_tokens_token_hash", table_name="password_reset_tokens"
    )
    op.drop_index(
        "ix_password_reset_tokens_user_id", table_name="password_reset_tokens"
    )
    op.drop_table("password_reset_tokens")
