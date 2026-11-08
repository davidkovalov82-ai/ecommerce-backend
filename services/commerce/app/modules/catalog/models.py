from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
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
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        default=1,
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
