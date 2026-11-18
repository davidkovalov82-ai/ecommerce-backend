"""add_shipments_and_order_shipping_address

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-11-09 20:00:00.000000

Downgrade policy:
Downgrade is explicitly prohibited if there are any shipment events
(`shipment_id IS NOT NULL` or `event_type = 'shipment.shipped'`) in `order_outbox_events`.
The legacy unique constraint `(order_id, event_type)` cannot support multiple
shipment events per order. A descriptive `RuntimeError` is raised before making
any schema changes, preserving existing data and state. If no shipment events exist,
downgrade safely proceeds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Колони за адрес и fulfillment_status в orders
    op.add_column(
        "orders",
        sa.Column(
            "fulfillment_status",
            sa.String(30),
            nullable=False,
            server_default="unshipped",
        ),
    )
    op.create_check_constraint(
        "ck_orders_fulfillment_status_valid",
        "orders",
        "fulfillment_status IN ('unshipped', 'partially_shipped', 'shipped')",
    )
    op.add_column(
        "orders",
        sa.Column("shipping_recipient_name", sa.String(255), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("shipping_phone", sa.String(50), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("shipping_country_code", sa.String(2), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("shipping_city", sa.String(100), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("shipping_postal_code", sa.String(20), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("shipping_address_line1", sa.String(255), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("shipping_address_line2", sa.String(255), nullable=True),
    )

    # 2. Колона seller_id в order_items
    op.add_column(
        "order_items",
        sa.Column(
            "seller_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_order_items_seller_id",
        "order_items",
        ["seller_id"],
    )

    # 3. Таблица shipments
    op.create_table(
        "shipments",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "seller_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="unshipped",
        ),
        sa.Column(
            "shipped_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "order_id",
            "seller_id",
            name="uq_shipments_order_id_seller_id",
        ),
        sa.CheckConstraint(
            "status IN ('unshipped', 'shipped')",
            name="ck_shipments_status_valid",
        ),
    )
    op.create_index(
        "ix_shipments_seller_id_created",
        "shipments",
        ["seller_id", "created_at"],
    )
    op.create_index(
        "ix_shipments_order_id",
        "shipments",
        ["order_id"],
    )

    # 4. Модификация на order_outbox_events
    op.add_column(
        "order_outbox_events",
        sa.Column(
            "shipment_id",
            sa.BigInteger(),
            sa.ForeignKey("shipments.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_order_outbox_shipment_id",
        "order_outbox_events",
        ["shipment_id"],
    )
    op.drop_constraint(
        "uq_order_outbox_order_id_event_type",
        "order_outbox_events",
        type_="unique",
    )
    op.create_index(
        "uq_order_outbox_order_event",
        "order_outbox_events",
        ["order_id", "event_type"],
        unique=True,
        postgresql_where=sa.text("shipment_id IS NULL"),
    )
    op.create_index(
        "uq_order_outbox_shipment_event",
        "order_outbox_events",
        ["shipment_id", "event_type"],
        unique=True,
        postgresql_where=sa.text("shipment_id IS NOT NULL"),
    )


def downgrade() -> None:
    # 0. Проверка за съществуващи shipment събития преди всякакви промени по схемата
    bind = op.get_bind()
    count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM order_outbox_events "
            "WHERE shipment_id IS NOT NULL OR event_type = 'shipment.shipped'"
        )
    ).scalar()
    if count and count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration {revision}: found {count} shipment events in order_outbox_events. "
            "The legacy unique constraint (order_id, event_type) is incompatible with multi-shipment events. "
            "Downgrade is blocked to protect event data and integrity."
        )

    # 4. Връщане на order_outbox_events
    op.drop_index("uq_order_outbox_shipment_event", table_name="order_outbox_events")
    op.drop_index("uq_order_outbox_order_event", table_name="order_outbox_events")
    op.create_unique_constraint(
        "uq_order_outbox_order_id_event_type",
        "order_outbox_events",
        ["order_id", "event_type"],
    )
    op.drop_index("ix_order_outbox_shipment_id", table_name="order_outbox_events")
    op.drop_column("order_outbox_events", "shipment_id")

    # 3. Изтриване на shipments
    op.drop_index("ix_shipments_order_id", table_name="shipments")
    op.drop_index("ix_shipments_seller_id_created", table_name="shipments")
    op.drop_table("shipments")

    # 2. Премахване на seller_id от order_items
    op.drop_index("ix_order_items_seller_id", table_name="order_items")
    op.drop_column("order_items", "seller_id")

    # 1. Премахване на адрес и fulfillment_status от orders
    op.drop_column("orders", "shipping_address_line2")
    op.drop_column("orders", "shipping_address_line1")
    op.drop_column("orders", "shipping_postal_code")
    op.drop_column("orders", "shipping_city")
    op.drop_column("orders", "shipping_country_code")
    op.drop_column("orders", "shipping_phone")
    op.drop_column("orders", "shipping_recipient_name")
    op.drop_constraint("ck_orders_fulfillment_status_valid", "orders", type_="check")
    op.drop_column("orders", "fulfillment_status")
