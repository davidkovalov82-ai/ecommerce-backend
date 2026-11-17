"""Репозитори за директен достъп до PostgreSQL таблицата cart_items."""

from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import CartItem


class CartRepository:
    """Управлява записите за потребителски колички в изолираната база на Cart."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_by_user_id(self, user_id: int) -> Sequence[CartItem]:
        """Връща всички позиции за даден потребител, подредени по product_id възходящо."""
        stmt = (
            select(CartItem)
            .where(CartItem.user_id == user_id)
            .order_by(CartItem.product_id.asc())
        )
        return self._session.scalars(stmt).all()

    def get_item(self, user_id: int, product_id: int) -> CartItem | None:
        """Извлича конкретен продукт от количката на потребителя."""
        return self._session.get(CartItem, (user_id, product_id))

    def upsert_item(self, user_id: int, product_id: int, quantity: int) -> CartItem:
        """Вмъква нова позиция или обновява количеството при съществуваща такава."""
        stmt = (
            insert(CartItem)
            .values(
                user_id=user_id,
                product_id=product_id,
                quantity=quantity,
            )
            .on_conflict_do_update(
                index_elements=[CartItem.user_id, CartItem.product_id],
                set_={"quantity": quantity, "updated_at": func.now()},
            )
            .returning(CartItem)
        )
        item = self._session.scalars(stmt).one()
        return item

    def delete_item(self, user_id: int, product_id: int) -> bool:
        """Изтрива конкретен продукт от количката. Връща True, ако е намерен и изтрит."""
        stmt = delete(CartItem).where(
            CartItem.user_id == user_id,
            CartItem.product_id == product_id,
        )
        result = self._session.execute(stmt)
        return result.rowcount > 0

    def delete_items(self, user_id: int, product_ids: list[int]) -> int:
        """Изтрива група закупени продукти от количката на потребителя."""
        if not product_ids:
            return 0
        stmt = delete(CartItem).where(
            CartItem.user_id == user_id,
            CartItem.product_id.in_(product_ids),
        )
        result = self._session.execute(stmt)
        return result.rowcount

    def clear_by_user_id(self, user_id: int) -> int:
        """Премахва всички позиции от количката на потребител."""
        stmt = delete(CartItem).where(CartItem.user_id == user_id)
        result = self._session.execute(stmt)
        return result.rowcount
