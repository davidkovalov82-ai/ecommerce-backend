from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.catalog.models import Product


class ProductRepository:
    """Хранилище за достъп до данни за продукти."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, product_id: int) -> Product | None:
        """Извлича продукт по неговото ID от PostgreSQL базата данни."""
        return self._session.get(Product, product_id)

    def list_active(self, limit: int, offset: int) -> Sequence[Product]:
        """Извлича списък с активни продукти, подредени по ID възходящо с пагинация."""
        stmt = (
            select(Product)
            .where(Product.active.is_(True))
            .order_by(Product.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return self._session.scalars(stmt).all()
