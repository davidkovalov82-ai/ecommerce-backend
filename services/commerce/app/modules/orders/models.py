import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Order(Base):
    """SQLAlchemy ORM модел за поръчки."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_orders_user_id_idempotency_key",
        ),
        CheckConstraint(
            "status = 'pending_payment'",
            name="ck_orders_status_valid",
        ),
        CheckConstraint("currency = 'EUR'", name="ck_orders_currency_eur"),
        CheckConstraint(
            "total_minor >= 0",
            name="ck_orders_total_minor_non_negative",
        ),
        Index("ix_orders_user_created_id", "user_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="pending_payment",
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="EUR",
    )
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )

    items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItem.product_id.asc()",
    )


class OrderItem(Base):
    """SQLAlchemy ORM модел за позициите в поръчката със запазени стойности."""

    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "product_id",
            name="uq_order_items_order_id_product_id",
        ),
        CheckConstraint(
            "quantity >= 1 AND quantity <= 99",
            name="ck_order_items_quantity_range",
        ),
        CheckConstraint(
            "unit_price_minor >= 0",
            name="ck_order_items_unit_price_minor_non_negative",
        ),
        CheckConstraint(
            "subtotal_minor >= 0",
            name="ck_order_items_subtotal_minor_non_negative",
        ),
        Index("ix_order_items_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_title: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="items")
