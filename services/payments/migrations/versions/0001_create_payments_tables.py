"""create payments tables

Revision ID: 0001
Revises:
Create Date: 2026-11-10 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Създава началните таблици в изолираната Payments база данни."""
    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_key", sa.String(length=100), nullable=False),
        sa.Column("payment_method", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "mode",
            sa.String(length=10),
            server_default="test",
            nullable=False,
        ),
        sa.Column("line_items", sa.JSON(), nullable=False),
        sa.Column("order_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('initiated', 'open', 'processing', 'succeeded', 'cancelling', 'failed', 'expired', 'declined')",
            name="ck_payment_attempts_status_valid",
        ),
        sa.CheckConstraint(
            "amount_minor >= 0",
            name="ck_payment_attempts_amount_minor_non_negative",
        ),
        sa.CheckConstraint("currency = 'EUR'", name="ck_payment_attempts_currency_eur"),
        sa.CheckConstraint(
            "mode IN ('test', 'live')", name="ck_payment_attempts_mode_valid"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "order_id",
            "attempt_key",
            name="uq_payment_attempts_order_id_attempt_key",
        ),
    )
    op.create_index(
        "ix_payment_attempts_order_id",
        "payment_attempts",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        "ix_payment_attempts_status_expires",
        "payment_attempts",
        ["status", "order_expires_at"],
        unique=False,
    )
    op.create_index(
        "uq_payment_attempts_one_succeeded_per_order",
        "payment_attempts",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    op.create_index(
        "uq_payment_attempts_one_active_per_order",
        "payment_attempts",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('initiated', 'open', 'processing', 'cancelling')"
        ),
    )

    op.create_table(
        "stripe_checkout_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("payment_attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("stripe_session_id", sa.String(length=255), nullable=True),
        sa.Column("checkout_url", sa.String(length=1024), nullable=True),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column(
            "mode",
            sa.String(length=10),
            server_default="test",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('initiated', 'open', 'processing', 'completed', 'cancelling', 'expired', 'failed')",
            name="ck_stripe_checkout_sessions_status_valid",
        ),
        sa.CheckConstraint(
            "mode IN ('test', 'live')", name="ck_stripe_checkout_sessions_mode_valid"
        ),
        sa.CheckConstraint(
            "amount_minor >= 0",
            name="ck_stripe_checkout_sessions_amount_minor_non_negative",
        ),
        sa.CheckConstraint(
            "currency = 'EUR'", name="ck_stripe_checkout_sessions_currency_eur"
        ),
        sa.ForeignKeyConstraint(
            ["payment_attempt_id"],
            ["payment_attempts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "payment_attempt_id",
            name="uq_stripe_checkout_sessions_attempt_id",
        ),
        sa.UniqueConstraint(
            "stripe_session_id",
            name="uq_stripe_checkout_sessions_session_id",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_stripe_checkout_sessions_idempotency_key",
        ),
    )
    op.create_index(
        "ix_stripe_checkout_sessions_order_id",
        "stripe_checkout_sessions",
        ["order_id"],
        unique=False,
    )

    op.create_table(
        "stripe_processed_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stripe_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("payment_attempt_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stripe_event_id",
            name="uq_stripe_processed_events_event_id",
        ),
    )
    op.create_index(
        "ix_stripe_processed_events_order_id",
        "stripe_processed_events",
        ["order_id"],
        unique=False,
    )

    op.create_table(
        "payment_outbox_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "event_id",
            sa.String(length=36),
            server_default=sa.text("gen_random_uuid()::text"),
            nullable=False,
        ),
        sa.Column(
            "event_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("payment_attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('payment.succeeded', 'payment.processing', 'payment.failed', 'payment.expired')",
            name="ck_payment_outbox_event_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'published', 'failed')",
            name="ck_payment_outbox_status_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_payment_outbox_event_id"),
        sa.UniqueConstraint(
            "payment_attempt_id",
            "event_type",
            name="uq_payment_outbox_attempt_event",
        ),
    )
    op.create_index(
        "ix_payment_outbox_status_created",
        "payment_outbox_events",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_outbox_order_id",
        "payment_outbox_events",
        ["order_id"],
        unique=False,
    )


def downgrade() -> None:
    """Премахва началните таблици на Payments."""
    op.drop_index("ix_payment_outbox_order_id", table_name="payment_outbox_events")
    op.drop_index(
        "ix_payment_outbox_status_created", table_name="payment_outbox_events"
    )
    op.drop_table("payment_outbox_events")
    op.drop_index(
        "ix_stripe_processed_events_order_id", table_name="stripe_processed_events"
    )
    op.drop_table("stripe_processed_events")
    op.drop_index(
        "ix_stripe_checkout_sessions_order_id", table_name="stripe_checkout_sessions"
    )
    op.drop_table("stripe_checkout_sessions")
    op.drop_index(
        "uq_payment_attempts_one_active_per_order", table_name="payment_attempts"
    )
    op.drop_index(
        "uq_payment_attempts_one_succeeded_per_order", table_name="payment_attempts"
    )
    op.drop_index("ix_payment_attempts_status_expires", table_name="payment_attempts")
    op.drop_index("ix_payment_attempts_order_id", table_name="payment_attempts")
    op.drop_table("payment_attempts")
