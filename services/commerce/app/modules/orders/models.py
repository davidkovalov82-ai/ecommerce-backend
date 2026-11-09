import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
            "status IN ('pending_payment', 'payment_processing', 'paid', 'cancelled', 'expired')",
            name="ck_orders_status_valid",
        ),
        CheckConstraint(
            "fulfillment_status IN ('unshipped', 'partially_shipped', 'shipped')",
            name="ck_orders_fulfillment_status_valid",
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
    fulfillment_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="unshipped",
        server_default="unshipped",
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="EUR",
    )
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    shipping_recipient_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    shipping_phone: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    shipping_country_code: Mapped[str | None] = mapped_column(
        String(2),
        nullable=True,
    )
    shipping_city: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    shipping_postal_code: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    shipping_address_line1: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    shipping_address_line2: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItem.product_id.asc()",
    )
    shipments: Mapped[list["Shipment"]] = relationship(
        "Shipment",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="Shipment.id.asc()",
    )
    payment_attempts: Mapped[list["PaymentAttempt"]] = relationship(
        "PaymentAttempt",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="PaymentAttempt.id.asc()",
    )
    stripe_sessions: Mapped[list["StripeCheckoutSession"]] = relationship(
        "StripeCheckoutSession",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="StripeCheckoutSession.id.asc()",
    )

    @property
    def shipping_address(self) -> dict[str, Any] | None:
        """Връща адреса за доставка като речник за сериализация или None за стари поръчки."""
        if self.shipping_recipient_name is not None:
            return {
                "recipient_name": self.shipping_recipient_name,
                "phone": self.shipping_phone or "",
                "country_code": self.shipping_country_code or "",
                "city": self.shipping_city or "",
                "postal_code": self.shipping_postal_code or "",
                "address_line1": self.shipping_address_line1 or "",
                "address_line2": self.shipping_address_line2,
            }
        return None


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
    seller_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    product_title: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="items")


class Shipment(Base):
    """SQLAlchemy ORM модел за пратка към поръчка от конкретен продавач."""

    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "seller_id",
            name="uq_shipments_order_id_seller_id",
        ),
        CheckConstraint(
            "status IN ('unshipped', 'shipped')",
            name="ck_shipments_status_valid",
        ),
        Index("ix_shipments_seller_id_created", "seller_id", "created_at"),
        Index("ix_shipments_order_id", "order_id"),
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
    seller_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="unshipped",
        server_default="unshipped",
    )
    shipped_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )

    order: Mapped["Order"] = relationship("Order", back_populates="shipments")

    @property
    def items(self) -> list["OrderItem"]:
        """Връща позициите от поръчката, принадлежащи на продавача на тази пратка."""
        if self.order and self.order.items:
            return [it for it in self.order.items if it.seller_id == self.seller_id]
        return []

    @property
    def items_count(self) -> int:
        """Връща броя позиции в пратката на продавача."""
        return len(self.items)

    @property
    def shipping_address(self) -> dict[str, Any] | None:
        """Връща адреса за доставка от поръчката."""
        if self.order:
            return self.order.shipping_address
        return None


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


class StripeCheckoutSession(Base):
    """SQLAlchemy ORM модел за Stripe Checkout сесии."""

    __tablename__ = "stripe_checkout_sessions"
    __table_args__ = (
        UniqueConstraint(
            "stripe_session_id",
            name="uq_stripe_checkout_sessions_session_id",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_stripe_checkout_sessions_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('initiated', 'open', 'processing', 'completed', 'cancelling', 'expired', 'failed')",
            name="ck_stripe_checkout_sessions_status_valid",
        ),
        CheckConstraint(
            "mode IN ('test', 'live')",
            name="ck_stripe_checkout_sessions_mode_valid",
        ),
        CheckConstraint(
            "amount_minor >= 0",
            name="ck_stripe_checkout_sessions_amount_minor_non_negative",
        ),
        CheckConstraint(
            "currency = 'EUR'",
            name="ck_stripe_checkout_sessions_currency_eur",
        ),
        Index("ix_stripe_checkout_sessions_order_id", "order_id"),
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
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="initiated",
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="EUR",
    )
    stripe_session_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    checkout_url: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="test",
        server_default="test",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.datetime.now(datetime.UTC),
    )

    order: Mapped["Order"] = relationship(
        "Order",
        back_populates="stripe_sessions",
    )


class StripeProcessedEvent(Base):
    """SQLAlchemy ORM модел за обработени Stripe webhook събития с цел идемпотентност."""

    __tablename__ = "stripe_processed_events"
    __table_args__ = (
        UniqueConstraint(
            "stripe_event_id",
            name="uq_stripe_processed_events_event_id",
        ),
        Index("ix_stripe_processed_events_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    stripe_event_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )


class OrderOutboxEvent(Base):
    """SQLAlchemy ORM модел за Outbox събития (Transactional Outbox)."""

    __tablename__ = "order_outbox_events"
    __table_args__ = (
        Index(
            "uq_order_outbox_order_event",
            "order_id",
            "event_type",
            unique=True,
            postgresql_where=text("shipment_id IS NULL"),
        ),
        Index(
            "uq_order_outbox_shipment_event",
            "shipment_id",
            "event_type",
            unique=True,
            postgresql_where=text("shipment_id IS NOT NULL"),
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name="ck_order_outbox_status_valid",
        ),
        Index("ix_order_outbox_status_created", "status", "created_at"),
        Index("ix_order_outbox_order_id", "order_id"),
        Index("ix_order_outbox_shipment_id", "shipment_id"),
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
    shipment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("shipments.id", ondelete="CASCADE"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="order.paid",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    last_error: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )
    processed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
