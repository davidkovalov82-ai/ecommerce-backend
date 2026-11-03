from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.modules.catalog.models import Product
from app.modules.catalog.repository import ProductRepository


class ProductNotFound(Exception):
    """Сигнализира, че търсеният продукт не съществува в каталога."""


class CatalogService:
    """Услуга за бизнес логика и сценарии в каталога с продукти."""

    def __init__(self, product_repository: ProductRepository, session: Session) -> None:
        self._product_repository = product_repository
        self._session = session

    def create_product(
        self,
        title: str,
        price_minor: int,
        currency: str,
        stock: int,
        active: bool = True,
    ) -> Product:
        """Сценарий: създаване на нов продукт в каталога.

        Управлява транзакционната граница (commit/rollback).
        """
        product = Product(
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

    def get_product_by_id(self, product_id: int) -> Product:
        """Сценарий: намиране на активен продукт по ID.

        Хвърля ProductNotFound, ако продуктът липсва в базата данни или не е активен.
        """
        product = self._product_repository.get_by_id(product_id)
        if product is None or not product.active:
            raise ProductNotFound(f"Product with id {product_id} not found")
        return product

    def list_active_products(self, limit: int, offset: int) -> Sequence[Product]:
        """Сценарий: извличане на публичен списък с активни продукти с пагинация."""
        return self._product_repository.list_active(limit=limit, offset=offset)
