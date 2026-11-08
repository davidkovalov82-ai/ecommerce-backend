from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.modules.catalog.models import Product
from app.modules.catalog.repository import ProductRepository


class ProductNotFound(Exception):
    """Сигнализира, че търсеният продукт не съществува в каталога или не принадлежи на потребителя."""


class CatalogService:
    """Услуга за бизнес логика и сценарии в каталога с продукти."""

    def __init__(self, product_repository: ProductRepository, session: Session) -> None:
        self._product_repository = product_repository
        self._session = session

    def create_product(
        self,
        seller_id: int,
        title: str,
        price_minor: int,
        currency: str,
        stock: int,
        active: bool = True,
    ) -> Product:
        """Сценарий: създаване на нов продукт в каталога от текущия потребител.

        Управлява транзакционната граница (commit/rollback).
        """
        product = Product(
            seller_id=seller_id,
            title=title,
            price_minor=price_minor,
            currency=currency,
            stock=stock,
            active=active,
        )
        try:
            self._product_repository.create(product)
            self._session.commit()
            return product
        except Exception:
            self._session.rollback()
            raise

    def update_product(
        self,
        product_id: int,
        seller_id: int,
        update_data: dict[str, Any],
    ) -> Product:
        """Сценарий: потребител редактира, деактивира или активира собствен продукт по ID.

        За чужд или несъществуващ продукт хвърля ProductNotFound (връща 404).
        Управлява транзакционната граница (commit/rollback).
        """
        product = self._product_repository.get_by_id(product_id)
        if product is None or product.seller_id != seller_id:
            raise ProductNotFound(f"Product with id {product_id} not found")

        try:
            for field, value in update_data.items():
                if field == "seller_id":
                    continue
                setattr(product, field, value)
            self._session.commit()
            return product
        except Exception:
            self._session.rollback()
            raise

    def get_product_by_id(self, product_id: int) -> Product:
        """Сценарий: намиране на активен продукт по ID за публичния каталог.

        Хвърля ProductNotFound, ако продуктът липсва в базата данни, не е активен или продавачът не е активен.
        """
        product = self._product_repository.get_active_by_id(product_id)
        if product is None:
            raise ProductNotFound(f"Product with id {product_id} not found")
        return product

    def list_active_products(self, limit: int, offset: int) -> Sequence[Product]:
        """Сценарий: извличане на публичен списък с активни продукти с пагинация."""
        return self._product_repository.list_active(limit=limit, offset=offset)

    def list_products_by_seller(
        self, seller_id: int, limit: int, offset: int
    ) -> Sequence[Product]:
        """Сценарий: извличане на продукти на конкретен продавач (вкл. неактивни) с пагинация."""
        return self._product_repository.list_by_seller(
            seller_id=seller_id, limit=limit, offset=offset
        )
