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
    text,
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
            "status IN ('pending_payment', 'paid', 'cancelled')",
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
    payment_attempts: Mapped[list["PaymentAttempt"]] = relationship(
        "PaymentAttempt",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="PaymentAttempt.id.asc()",
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


class PaymentAttempt(Base):
    """SQLAlchemy ORM модел за опити за плащане на поръчка."""

    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "idempotency_key",
            name="uq_payment_attempts_order_id_idempotency_key",
        ),
        Index(
            "uq_payment_attempts_one_succeeded_per_order",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
        ),
        CheckConstraint(
            "status IN ('succeeded', 'declined')",
            name="ck_payment_attempts_status_valid",
        ),
        CheckConstraint(
            "amount_minor >= 0",
            name="ck_payment_attempts_amount_minor_non_negative",
        ),
        CheckConstraint("currency = 'EUR'", name="ck_payment_attempts_currency_eur"),
        Index("ix_payment_attempts_order_id", "order_id"),
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
    payment_method: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="EUR",
    )
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )

    order: Mapped["Order"] = relationship(
        "Order",
        back_populates="payment_attempts",
    )

