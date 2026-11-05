from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.modules.cart.models import CartItem


class CartRepository:
    """Хранилище за достъп до данни за потребителските колички."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_by_user_id(self, user_id: int) -> Sequence[CartItem]:
        """Извлича всички позиции в количката на потребителя, подредени по product_id възходящо."""
        stmt = (
            select(CartItem)
            .where(CartItem.user_id == user_id)
            .order_by(CartItem.product_id.asc())
        )
        return self._session.scalars(stmt).all()

    def get_item(self, user_id: int, product_id: int) -> CartItem | None:
        """Извлича конкретна позиция от количката по съставния първичен ключ (user_id, product_id)."""
        return self._session.get(CartItem, (user_id, product_id))

    def upsert_item(self, user_id: int, product_id: int, quantity: int) -> None:
        """Атомарен PostgreSQL upsert за позиция в количката чрез ON CONFLICT DO UPDATE."""
        stmt = (
            insert(CartItem)
            .values(user_id=user_id, product_id=product_id, quantity=quantity)
            .on_conflict_do_update(
                index_elements=["user_id", "product_id"],
                set_={"quantity": quantity},
            )
        )
        self._session.execute(stmt)

    def delete_item(self, user_id: int, product_id: int) -> bool:
        """Премахва позиция от количката на потребителя. Връща True, ако е съществувал такъв ред."""
        stmt = delete(CartItem).where(
            CartItem.user_id == user_id,
            CartItem.product_id == product_id,
        )
        result = self._session.execute(stmt)
        return bool(result.rowcount and result.rowcount > 0)
