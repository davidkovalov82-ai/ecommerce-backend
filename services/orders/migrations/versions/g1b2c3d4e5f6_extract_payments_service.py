"""extract payments service inbox discrepancy and attempt tracking

Revision ID: g1b2c3d4e5f6
Revises: f7a8b9c0d1e2
Create Date: 2026-11-10 10:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Подготвя Commerce за събитийната интеграция с Payments без триене на старите таблици."""
    op.add_column(
        "orders",
        sa.Column(
            "payment_attempt_seq",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "active_attempt_key",
            sa.String(length=100),
            nullable=True,
        ),
    )

    # Попълваме active_attempt_key и payment_attempt_seq според последната стара Stripe сесия:
    # - при незавършена/активна (open/initiated/processing/cancelling/completed) запазваме нейния ключ;
    # - при последна failed/expired сесия задаваме по-голям seq и свеж ключ за следващия checkout.
    op.execute(
        sa.text(
            """
            WITH latest_session AS (
                SELECT DISTINCT ON (s.order_id)
                    s.order_id,
                    s.status,
                    s.idempotency_key
                FROM stripe_checkout_sessions s
                ORDER BY s.order_id, s.id DESC
            ),
            order_seq_bounds AS (
                SELECT
                    o.id AS order_id,
                    GREATEST(
                        COALESCE(sc.cnt, 0),
                        COALESCE(sc.max_suffix, 0),
                        COALESCE(pa.max_suffix, 0),
                        1
                    ) AS highest_seq
                FROM orders o
                LEFT JOIN (
                    SELECT
                        s.order_id,
                        COUNT(*) AS cnt,
                        MAX(
                            CASE
                                WHEN s.idempotency_key ~ '^order_[0-9]+_attempt_[0-9]{1,9}$'
                                THEN CAST(
                                    (regexp_match(s.idempotency_key, '^order_[0-9]+_attempt_([0-9]{1,9})$'))[1]
                                    AS INTEGER
                                )
                                ELSE 0
                            END
                        ) AS max_suffix
                    FROM stripe_checkout_sessions s
                    GROUP BY s.order_id
                ) sc ON sc.order_id = o.id
                LEFT JOIN (
                    SELECT
                        p.order_id,
                        MAX(
                            CASE
                                WHEN p.idempotency_key ~ '^order_[0-9]+_attempt_[0-9]{1,9}$'
                                THEN CAST(
                                    (regexp_match(p.idempotency_key, '^order_[0-9]+_attempt_([0-9]{1,9})$'))[1]
                                    AS INTEGER
                                )
                                ELSE 0
                            END
                        ) AS max_suffix
                    FROM payment_attempts p
                    GROUP BY p.order_id
                ) pa ON pa.order_id = o.id
            )
            UPDATE orders o
            SET
                payment_attempt_seq = CASE
                    WHEN ls.order_id IS NULL THEN 1
                    WHEN ls.status IN ('failed', 'expired') THEN osb.highest_seq + 1
                    ELSE osb.highest_seq
                END,
                active_attempt_key = CASE
                    WHEN ls.order_id IS NULL THEN 'order_' || o.id::text || '_attempt_1'
                    WHEN ls.status IN ('failed', 'expired') THEN 'order_' || o.id::text || '_attempt_' || (osb.highest_seq + 1)::text
                    ELSE ls.idempotency_key
                END
            FROM order_seq_bounds osb
            LEFT JOIN latest_session ls ON ls.order_id = osb.order_id
            WHERE o.id = osb.order_id
            """
        )
    )

    op.create_table(
        "order_inbox_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("payment_attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_order_inbox_events_order_id",
        "order_inbox_events",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_inbox_events_attempt_id",
        "order_inbox_events",
        ["payment_attempt_id"],
        unique=False,
    )

    op.create_table(
        "order_payment_discrepancies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("payment_attempt_id", sa.BigInteger(), nullable=True),
        sa.Column("event_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("stripe_session_id", sa.String(length=255), nullable=True),
        sa.Column("stripe_event_id", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="open",
            nullable=False,
        ),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_order_payment_discrepancies_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            name="uq_order_payment_discrepancies_event_id",
        ),
    )
    op.create_index(
        "ix_order_payment_discrepancies_order_id",
        "order_payment_discrepancies",
        ["order_id"],
        unique=False,
    )


def downgrade() -> None:
    """Премахва К5 таблиците, само ако няма записани платежни разхождения."""
    conn = op.get_bind()
    open_discrepancies = conn.execute(
        sa.text("SELECT COUNT(*) FROM order_payment_discrepancies")
    ).scalar_one()
    if open_discrepancies > 0:
        raise RuntimeError(
            f"Cannot downgrade migration g1b2c3d4e5f6: found {open_discrepancies} payment discrepancies"
        )

    op.drop_index(
        "ix_order_payment_discrepancies_order_id",
        table_name="order_payment_discrepancies",
    )
    op.drop_table("order_payment_discrepancies")
    op.drop_index("ix_order_inbox_events_attempt_id", table_name="order_inbox_events")
    op.drop_index("ix_order_inbox_events_order_id", table_name="order_inbox_events")
    op.drop_table("order_inbox_events")
    op.drop_column("orders", "active_attempt_key")
    op.drop_column("orders", "payment_attempt_seq")
