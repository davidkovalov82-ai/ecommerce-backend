from collections.abc import Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.catalog.models import Category, Product
from app.modules.catalog.repository import CategoryRepository, ProductRepository


class ProductNotFound(Exception):
    """Сигнализира, че търсеният продукт не съществува в каталога или не принадлежи на потребителя."""


class CategoryNotFound(Exception):
    """Сигнализира, че категорията не съществува в базата данни."""


class CategoryNameAlreadyExists(Exception):
    """Сигнализира, че категория с такова име (case-insensitive и без външни интервали) вече съществува."""


class CategoryInUse(Exception):
    """Сигнализира, че категорията съдържа поне един продукт и не може да бъде изтрита."""


class InvalidCategoryId(Exception):
    """Сигнализира, че посоченият category_id не съществува."""


def is_unique_category_name_violation(exc: IntegrityError) -> bool:
    """Проверява дали IntegrityError е причинен от нарушение на уникалния индекс uq_categories_name_lower."""
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None)
    if pgcode == "23505":
        return True
    msg = str(exc).lower()
    return "uq_categories_name_lower" in msg or "unique constraint" in msg


class CatalogService:
    """Услуга за бизнес логика и сценарии в каталога с продукти и категории."""

    def __init__(
        self,
        product_repository: ProductRepository,
        session: Session,
        category_repository: CategoryRepository | None = None,
    ) -> None:
        self._product_repository = product_repository
        self._session = session
        self._category_repository = (
            category_repository
            if category_repository is not None
            else CategoryRepository(session)
        )

    def list_categories(self) -> Sequence[Category]:
        """Сценарий: извличане на всички категории със стабилна сортировка по ID възходящо."""
        return self._category_repository.list_all()

    def create_category(self, name: str) -> Category:
        """Сценарий: създаване на нова категория от администратор.

        Осигурява уникалност на името в PostgreSQL (case-insensitive и без крайни интервали).
        При конфликт хвърля CategoryNameAlreadyExists.
        """
        trimmed = name.strip()
        existing = self._category_repository.get_by_name(trimmed)
        if existing is not None:
            raise CategoryNameAlreadyExists(f"Category '{trimmed}' already exists")

        category = Category(name=trimmed)
        try:
            self._category_repository.create(category)
            self._session.commit()
            return category
        except IntegrityError as exc:
            self._session.rollback()
            if is_unique_category_name_violation(exc):
                raise CategoryNameAlreadyExists(
                    f"Category '{trimmed}' already exists"
                ) from exc
            raise
        except Exception:
            self._session.rollback()
            raise

    def update_category(self, category_id: int, name: str) -> Category:
        """Сценарий: преименуване на съществуваща категория от администратор.

        Ако категорията не съществува, хвърля CategoryNotFound.
        Ако новото име вече принадлежи на друга категория, хвърля CategoryNameAlreadyExists.
        """
        category = self._category_repository.get_by_id(category_id)
        if category is None:
            raise CategoryNotFound(f"Category with id {category_id} not found")

        trimmed = name.strip()
        existing = self._category_repository.get_by_name(trimmed)
        if existing is not None and existing.id != category_id:
            raise CategoryNameAlreadyExists(f"Category '{trimmed}' already exists")

        category.name = trimmed
        try:
            self._session.commit()
            return category
        except IntegrityError as exc:
            self._session.rollback()
            if is_unique_category_name_violation(exc):
                raise CategoryNameAlreadyExists(
                    f"Category '{trimmed}' already exists"
                ) from exc
            raise
        except Exception:
            self._session.rollback()
            raise

    def delete_category(self, category_id: int) -> None:
        """Сценарий: изтриване на празна категория от администратор.

        Ако към категорията е привързан поне един продукт (дори неактивен),
        хвърля CategoryInUse и продуктите остават непокътнати.
        """
        category = self._category_repository.get_by_id(category_id)
        if category is None:
            raise CategoryNotFound(f"Category with id {category_id} not found")

        if self._product_repository.has_products_in_category(category_id):
            raise CategoryInUse(
                f"Category {category_id} is assigned to one or more products"
            )

        try:
            self._category_repository.delete(category)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise CategoryInUse(f"Category {category_id} is in use") from exc
        except Exception:
            self._session.rollback()
            raise

    def create_product(
        self,
        seller_id: int,
        title: str,
        price_minor: int,
        currency: str,
        stock: int,
        active: bool = True,
        category_id: int | None = None,
    ) -> Product:
        """Сценарий: създаване на нов продукт в каталога от текущия потребител.

        Ако е подаден несъществуващ category_id, хвърля InvalidCategoryId.
        Управлява транзакционната граница (commit/rollback).
        """
        if category_id is not None:
            category = self._category_repository.get_by_id(category_id)
            if category is None:
                raise InvalidCategoryId(f"Category with id {category_id} not found")

        product = Product(
            seller_id=seller_id,
            category_id=category_id,
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
        При несъществуващ category_id хвърля InvalidCategoryId (връща 422 без частични промени).
        Управлява транзакционната граница (commit/rollback).
        """
        product = self._product_repository.get_by_id(product_id)
        if product is None or product.seller_id != seller_id:
            raise ProductNotFound(f"Product with id {product_id} not found")

        # Първо валидираме category_id, преди да правим каквито и да е промени
        if "category_id" in update_data:
            target_category_id = update_data["category_id"]
            if target_category_id is not None:
                category = self._category_repository.get_by_id(target_category_id)
                if category is None:
                    raise InvalidCategoryId(
                        f"Category with id {target_category_id} not found"
                    )

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

    def list_active_products(
        self, limit: int, offset: int, category_id: int | None = None
    ) -> Sequence[Product]:
        """Сценарий: извличане на публичен списък с активни продукти с пагинация и филтър по категория."""
        return self._product_repository.list_active(
            limit=limit, offset=offset, category_id=category_id
        )

    def list_products_by_seller(
        self, seller_id: int, limit: int, offset: int
    ) -> Sequence[Product]:
        """Сценарий: извличане на продукти на конкретен продавач (вкл. неактивни) с пагинация."""
        return self._product_repository.list_by_seller(
            seller_id=seller_id, limit=limit, offset=offset
        )
