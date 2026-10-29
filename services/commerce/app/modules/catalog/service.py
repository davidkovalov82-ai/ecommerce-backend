from collections.abc import Sequence

from app.modules.catalog.models import Product
from app.modules.catalog.repository import ProductRepository


class ProductNotFound(Exception):
    """Сигнализира, че търсеният продукт не съществува в каталога."""


class CatalogService:
    """Услуга за бизнес логика и сценарии в каталога с продукти."""

    def __init__(self, product_repository: ProductRepository) -> None:
        self._product_repository = product_repository

    def get_product_by_id(self, product_id: int) -> Product:
        """Сценарий: намиране на продукт по ID.

        Хвърля ProductNotFound, ако продуктът липсва в базата данни.
        """
        product = self._product_repository.get_by_id(product_id)
        if product is None:
            raise ProductNotFound(f"Product with id {product_id} not found")
        return product

    def list_active_products(self, limit: int, offset: int) -> Sequence[Product]:
        """Сценарий: извличане на публичен списък с активни продукти с пагинация."""
        return self._product_repository.list_active(limit=limit, offset=offset)
