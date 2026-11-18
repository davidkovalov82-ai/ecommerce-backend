"""Репозитори за директен достъп до PostgreSQL таблицата cart_items."""

from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import CartClearedOrder, CartItem


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
        """Вмъква нова позиция или обновява количеството с нова монотонна версия от sequence."""
        stmt = (
            insert(CartItem)
            .values(
                user_id=user_id,
                product_id=product_id,
                quantity=quantity,
                version=func.nextval("cart_item_version_seq"),
            )
            .on_conflict_do_update(
                index_elements=[CartItem.user_id, CartItem.product_id],
                set_={
                    "quantity": quantity,
                    "version": func.nextval("cart_item_version_seq"),
                    "updated_at": func.now(),
                },
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

    def is_order_cleared(self, order_id: int) -> bool:
        """Проверява дали поръчка с даден order_id вече е обработена от Cart."""
        stmt = select(CartClearedOrder).where(CartClearedOrder.order_id == order_id)
        return self._session.scalar(stmt) is not None

    def mark_order_cleared(self, order_id: int, user_id: int) -> None:
        """Записва order_id като обработен за осигуряване на идемпотентност."""
        stmt = (
            insert(CartClearedOrder)
            .values(order_id=order_id, user_id=user_id)
            .on_conflict_do_nothing()
        )
        self._session.execute(stmt)

    def delete_items_versioned(
        self, user_id: int, items: list[tuple[int, int]]
    ) -> list[int]:
        """Изтрива позициите само ако версията им в базата съвпада със снимката на поръчката.

        Ако потребителят е променил количеството по време на checkout (по-висока версия)
        или позицията е била пресъздадена наново (нова версия от sequence), тя НЕ се изтрива.
        """
        cleared_ids: list[int] = []
        for product_id, expected_version in items:
            stmt = delete(CartItem).where(
                CartItem.user_id == user_id,
                CartItem.product_id == product_id,
                CartItem.version == expected_version,
            )
            res = self._session.execute(stmt)
            if res.rowcount > 0:
                cleared_ids.append(product_id)
        return cleared_ids

    def clear_by_user_id(self, user_id: int) -> int:
        """Премахва всички позиции от количката на потребител."""
        stmt = delete(CartItem).where(CartItem.user_id == user_id)
        result = self._session.execute(stmt)
        return result.rowcount
