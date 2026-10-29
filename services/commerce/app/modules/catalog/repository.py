from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.catalog.models import Product


def get_by_id(session: Session, product_id: int) -> Product | None:
    """Извлича продукт по неговото ID от PostgreSQL базата данни."""
    return session.get(Product, product_id)


def list_active(session: Session, limit: int, offset: int) -> Sequence[Product]:
    """Извлича списък с активни продукти, подредени по ID възходящо с пагинация."""
    stmt = (
        select(Product)
        .where(Product.active.is_(True))
        .order_by(Product.id.asc())
        .limit(limit)
        .offset(offset)
    )
    return session.scalars(stmt).all()
