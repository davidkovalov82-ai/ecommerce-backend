"""SQLAlchemy ORM модели за Catalog."""

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Category(Base):
    """SQLAlchemy ORM модел за продуктови категории."""

    __tablename__ = "categories"
    __table_args__ = (
        Index(
            "uq_categories_name_lower",
            text("lower(trim(name))"),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Product(Base):
    """SQLAlchemy ORM модел за таблицата с продукти."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(
            "price_minor >= 0", name="ck_products_price_minor_non_negative"
        ),
        CheckConstraint("stock >= 0", name="ck_products_stock_non_negative"),
        CheckConstraint("currency = 'EUR'", name="ck_products_currency_eur"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seller_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
    )
    category_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
        default=None,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SellerStatus(Base):
    """SQLAlchemy ORM модел за локална видимост на статус на продавач в каталога."""

    __tablename__ = "seller_statuses"

    seller_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )


class SellerInboxEvent(Base):
    """SQLAlchemy ORM модел за приети събития за статус на продавачи."""

    __tablename__ = "seller_inbox_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seller_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )


class StockReservation(Base):
    """SQLAlchemy ORM модел за резервации на складова наличност от поръчки."""

    __tablename__ = "stock_reservations"
    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "product_id",
            name="uq_stock_reservations_order_id_product_id",
        ),
        CheckConstraint("quantity > 0", name="ck_stock_reservations_qty_positive"),
        CheckConstraint(
            "status IN ('reserved', 'released')",
            name="ck_stock_reservations_status_valid",
        ),
        Index("ix_stock_reservations_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )
