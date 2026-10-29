from sqlalchemy.orm import Session

from app.modules.catalog.models import Product


def get_by_id(session: Session, product_id: int) -> Product | None:
    """Извлича продукт по неговото ID от PostgreSQL базата данни."""
    return session.get(Product, product_id)
