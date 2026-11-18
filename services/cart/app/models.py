"""SQLAlchemy ORM модели за Cart услугата."""

import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CartItem(Base):
    """SQLAlchemy ORM модел за позициите в потребителската количка.

    Cart съхранява единствено състава на количката (user_id, product_id) и количеството.
    Цените, наличността и метаданните се извличат динамично от Catalog.
    """

    __tablename__ = "cart_items"
    __table_args__ = (
        CheckConstraint(
            "quantity >= 1 AND quantity <= 99",
            name="ck_cart_items_quantity_range",
        ),
    )

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CartClearedOrder(Base):
    """Таблица за гарантиране на идемпотентност при изчистване на поръчани колички.

    Записва order_id, за който поръчката вече е изчистена, за да не се допуска
    повторно изтриване или грешки при retry от Orders.
    """

    __tablename__ = "cart_cleared_orders"

    order_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    cleared_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
