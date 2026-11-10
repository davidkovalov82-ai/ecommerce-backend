from sqlalchemy import CheckConstraint, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CartItem(Base):
    """SQLAlchemy ORM модел за позициите в потребителската количка."""

    __tablename__ = "cart_items"
    __table_args__ = (
        CheckConstraint(
            "quantity >= 1 AND quantity <= 99",
            name="ck_cart_items_quantity_range",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
