from sqlalchemy.orm import Session

from app.modules.catalog import repository
from app.modules.catalog.models import Product


class ProductNotFound(Exception):
    """Сигнализира, че търсеният продукт не съществува в каталога."""


def get_product_by_id(session: Session, product_id: int) -> Product:
    """Сценарий: намиране на продукт по ID.

    Хвърля ProductNotFound, ако продуктът липсва в базата данни.
    """
    product = repository.get_by_id(session, product_id)
    if product is None:
        raise ProductNotFound(f"Product with id {product_id} not found")
    return product
