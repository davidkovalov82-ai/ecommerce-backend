from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.accounts.models import User
from app.modules.catalog.models import Product


class ProductRepository:
    """Хранилище за достъп до данни за продукти."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, product_id: int) -> Product | None:
        """Извлича продукт по неговото ID от PostgreSQL базата данни."""
        return self._session.get(Product, product_id)

    def get_active_by_id(self, product_id: int) -> Product | None:
        """Извлича активен продукт на активен продавач по неговото ID."""
        stmt = (
            select(Product)
            .join(User, Product.seller_id == User.id)
            .where(
                Product.id == product_id,
                Product.active.is_(True),
                User.status == "active",
            )
        )
        return self._session.scalars(stmt).one_or_none()

    def get_by_ids(self, product_ids: Sequence[int]) -> Sequence[Product]:
        """Извлича списък с продукти по техните ID-та чрез една IN заявка."""
        if not product_ids:
            return []
        stmt = select(Product).where(Product.id.in_(product_ids))
        return self._session.scalars(stmt).all()

    def get_by_ids_for_update(self, product_ids: Sequence[int]) -> Sequence[Product]:
        """Извлича и заключва продукти за промяна, сортирани по ID възходящо."""
        if not product_ids:
            return []
        sorted_ids = sorted(set(product_ids))
        stmt = (
            select(Product)
            .where(Product.id.in_(sorted_ids))
            .order_by(Product.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).all()

    def list_active(self, limit: int, offset: int) -> Sequence[Product]:
        """Извлича списък с активни продукти на активни продавачи, подредени по ID възходящо с пагинация."""
        stmt = (
            select(Product)
            .join(User, Product.seller_id == User.id)
            .where(Product.active.is_(True), User.status == "active")
            .order_by(Product.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return self._session.scalars(stmt).all()

    def list_by_seller(
        self, seller_id: int, limit: int, offset: int
    ) -> Sequence[Product]:
        """Извлича списък с продукти на конкретен продавач (вкл. неактивни), подредени по ID възходящо."""
        stmt = (
            select(Product)
            .where(Product.seller_id == seller_id)
            .order_by(Product.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return self._session.scalars(stmt).all()

    def create(self, product: Product) -> Product:
        """Добавя продукт в текущата сесия и прави flush, за да получи автоматично генерирано ID."""
        self._session.add(product)
        self._session.flush()
        return product
