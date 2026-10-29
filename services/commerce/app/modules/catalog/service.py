from typing import Any

from app.modules.catalog import repository


class ProductNotFound(Exception):
    """Сигнализира, че търсеният продукт не съществува в каталога."""


def get_product_by_id(product_id: int) -> dict[str, Any]:
    """Сценарий: намиране на продукт по ID.

    Хвърля ProductNotFound, ако продуктът липсва.
    """
    product = repository.get_by_id(product_id)
    if product is None:
        raise ProductNotFound(f"Product with id {product_id} not found")
    return product
