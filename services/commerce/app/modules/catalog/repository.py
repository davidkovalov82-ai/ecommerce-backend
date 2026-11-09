from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.accounts.models import User
from app.modules.catalog.models import Category, Product


class CategoryRepository:
    """Хранилище за достъп до данни за продуктови категории."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, category_id: int) -> Category | None:
        """Извлича категория по нейното ID от PostgreSQL базата данни."""
        return self._session.get(Category, category_id)

    def get_by_name(self, name: str) -> Category | None:
        """Извлича категория по нормализирано име (case-insensitive и без празни интервали)."""
        stmt = select(Category).where(
            func.lower(func.trim(Category.name)) == func.lower(func.trim(name))
        )
        return self._session.scalars(stmt).first()

    def list_all(self) -> Sequence[Category]:
        """Извлича всички категории със стабилна сортировка по ID възходящо."""
        stmt = select(Category).order_by(Category.id.asc())
        return self._session.scalars(stmt).all()

    def create(self, category: Category) -> Category:
        """Добавя категория в текущата сесия и прави flush."""
        self._session.add(category)
        self._session.flush()
        return category

    def delete(self, category: Category) -> None:
        """Изтрива категория от текущата сесия и прави flush."""
        self._session.delete(category)
        self._session.flush()


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

    def list_active(
        self, limit: int, offset: int, category_id: int | None = None
    ) -> Sequence[Product]:
        """Извлича списък с активни продукти на активни продавачи, подредени по ID възходящо с пагинация."""
        stmt = (
            select(Product)
            .join(User, Product.seller_id == User.id)
            .where(Product.active.is_(True), User.status == "active")
        )
        if category_id is not None:
            stmt = stmt.where(Product.category_id == category_id)
        stmt = stmt.order_by(Product.id.asc()).limit(limit).offset(offset)
        return self._session.scalars(stmt).all()

    def has_products_in_category(self, category_id: int) -> bool:
        """Проверява дали в дадена категория съществува поне един продукт (активен или неактивен)."""
        stmt = select(Product.id).where(Product.category_id == category_id).limit(1)
        return self._session.scalars(stmt).first() is not None

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
