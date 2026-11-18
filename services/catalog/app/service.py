"""Бизнес логика и сценарии за Catalog."""

import datetime
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Category, Product, StockReservation
from app.repository import (
    CategoryRepository,
    ProductRepository,
    ReservationRepository,
    SellerStatusRepository,
)
from app.schemas import ProductInternalDetails, StockReserveItem


class ProductNotFound(Exception):
    """Сигнализира, че продуктът не съществува или не е активен."""


class CategoryNotFound(Exception):
    """Сигнализира, че категорията не съществува."""


class CategoryNameAlreadyExists(Exception):
    """Сигнализира, че категория с такова име вече съществува."""


class CategoryInUse(Exception):
    """Сигнализира, че категорията съдържа поне един продукт."""


class InvalidCategoryId(Exception):
    """Сигнализира за невалиден category_id."""


class InsufficientStock(Exception):
    """Сигнализира, че складовата наличност не достига."""

    def __init__(self, product_id: int, message: str = "Insufficient stock") -> None:
        super().__init__(message)
        self.product_id = product_id


class ProductUnavailable(Exception):
    """Сигнализира, че продуктът е недостъпен или деактивиран."""

    def __init__(self, product_id: int, message: str = "Product unavailable") -> None:
        super().__init__(message)
        self.product_id = product_id


class ReservationConflict(Exception):
    """Сигнализира, че за същия order_id съществува резервация с различен състав или вече освободена."""


class InvalidReservationItems(Exception):
    """Сигнализира невалиден състав за резервация (празен списък или дублиращи се позиции)."""


def is_unique_category_name_violation(exc: IntegrityError) -> bool:
    """Проверява за нарушение на уникалния индекс uq_categories_name_lower."""
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None)
    if pgcode == "23505":
        return True
    msg = str(exc).lower()
    return "uq_categories_name_lower" in msg or "unique constraint" in msg


class CatalogService:
    """Услуга за управление на категории, продукти и наличности в Catalog."""

    def __init__(
        self,
        session: Session,
        product_repository: ProductRepository | None = None,
        category_repository: CategoryRepository | None = None,
        seller_status_repository: SellerStatusRepository | None = None,
        reservation_repository: ReservationRepository | None = None,
    ) -> None:
        self._session = session
        self._product_repository = product_repository or ProductRepository(session)
        self._category_repository = category_repository or CategoryRepository(session)
        self._seller_status_repository = (
            seller_status_repository or SellerStatusRepository(session)
        )
        self._reservation_repository = reservation_repository or ReservationRepository(
            session
        )

    # --- Категории ---

    def list_categories(self) -> Sequence[Category]:
        """Извлича всички категории със стабилна сортировка."""
        return self._category_repository.list_all()

    def create_category(self, name: str) -> Category:
        """Създаване на нова категория от администратор."""
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
        """Преименуване на съществуваща категория."""
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
        """Изтриване на празна категория."""
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

    # --- Продукти ---

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
        """Създаване на продукт от автентикиран потребител."""
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
        """Частично обновяване на собствен продукт."""
        product = self._product_repository.get_by_id(product_id)
        if product is None or product.seller_id != seller_id:
            raise ProductNotFound(f"Product with id {product_id} not found")

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
        """Извличане на активен продукт по ID за публичния каталог."""
        product = self._product_repository.get_active_by_id(product_id)
        if product is None:
            raise ProductNotFound(f"Product with id {product_id} not found")
        return product

    def list_active_products(
        self, limit: int, offset: int, category_id: int | None = None
    ) -> Sequence[Product]:
        """Публичен списък с активни продукти."""
        return self._product_repository.list_active(
            limit=limit, offset=offset, category_id=category_id
        )

    def list_products_by_seller(
        self, seller_id: int, limit: int, offset: int
    ) -> Sequence[Product]:
        """Списък с продукти на конкретен продавач."""
        return self._product_repository.list_by_seller(
            seller_id=seller_id, limit=limit, offset=offset
        )

    # --- Вътрешен междусервизен договор за Cart и Orders ---

    def get_products_internal_batch(
        self, product_ids: Sequence[int]
    ) -> list[ProductInternalDetails]:
        """Пакетно извличане на детайли за продукти с локален статус на продавачите."""
        if not product_ids:
            return []

        products = self._product_repository.get_by_ids(product_ids)
        seller_ids = [p.seller_id for p in products]
        statuses = self._seller_status_repository.get_by_ids(seller_ids)

        result: list[ProductInternalDetails] = []
        for p in products:
            status = statuses.get(p.seller_id, "active")
            is_seller_active = status == "active"
            result.append(
                ProductInternalDetails(
                    id=p.id,
                    seller_id=p.seller_id,
                    category_id=p.category_id,
                    title=p.title,
                    price_minor=p.price_minor,
                    currency=p.currency,
                    stock=p.stock,
                    active=p.active,
                    seller_active=is_seller_active,
                )
            )
        return result

    def get_product_internal(self, product_id: int) -> ProductInternalDetails | None:
        """Извлича вътрешни детайли за единичен продукт."""
        product = self._product_repository.get_by_id(product_id)
        if product is None:
            return None
        status_rec = self._seller_status_repository.get_by_id(product.seller_id)
        is_seller_active = (status_rec.status == "active") if status_rec else True
        return ProductInternalDetails(
            id=product.id,
            seller_id=product.seller_id,
            category_id=product.category_id,
            title=product.title,
            price_minor=product.price_minor,
            currency=product.currency,
            stock=product.stock,
            active=product.active,
            seller_active=is_seller_active,
        )

    def reserve_stock(self, order_id: int, items: Sequence[StockReserveItem]) -> None:
        """Атомарно и идемпотентно резервиране на наличност за поръчка.

        1. Валидира непразен състав и уникални product_id.
        2. Заключва транзакционно order_id (pg_advisory_xact_lock) за защита от паралелни първи резервации.
        3. Проверява дали за този order_id вече съществуват резервации:
           - Същият състав и всички са 'reserved' -> безопасен повтор (връща успех).
           - Различен състав или вече освободени -> хвърля ReservationConflict (409).
        4. Заключва засегнатите продукти във възходящ ред по ID с FOR UPDATE.
        5. Проверява активност, статус на продавача и достатъчна наличност.
        6. Намалява наличността (product.stock -= qty).
        7. Създава записи в stock_reservations със статус 'reserved'.
        8. Commit на транзакцията.
        """
        if not items:
            raise InvalidReservationItems("Reservation items cannot be empty")
        req_pids = [item.product_id for item in items]
        if len(req_pids) != len(set(req_pids)):
            raise InvalidReservationItems("Duplicate product_id in reservation items")

        try:
            self._session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtext('catalog_reserve_' || :oid))"
                ),
                {"oid": str(order_id)},
            )

            if self._reservation_repository.is_order_released(order_id):
                raise ReservationConflict(
                    f"Reservation for order {order_id} has already been released"
                )

            existing = self._reservation_repository.get_by_order_id_for_update(order_id)
            if existing:
                existing_map = {r.product_id: r.quantity for r in existing}
                requested_map = {item.product_id: item.quantity for item in items}
                if existing_map != requested_map:
                    raise ReservationConflict(
                        f"Reservation for order {order_id} exists with different items"
                    )
                if all(r.status == "reserved" for r in existing):
                    return
                raise ReservationConflict(
                    f"Reservation for order {order_id} has already been released"
                )

            sorted_product_ids = sorted({item.product_id for item in items})
            locked_products = self._product_repository.get_by_ids_for_update(
                sorted_product_ids
            )
            products_by_id = {p.id: p for p in locked_products}

            seller_ids = [p.seller_id for p in locked_products]
            seller_statuses = self._seller_status_repository.get_by_ids(seller_ids)

            # Проверка на наличност и статус
            for item in items:
                prod = products_by_id.get(item.product_id)
                if prod is None or not prod.active:
                    raise ProductUnavailable(item.product_id)

                status = seller_statuses.get(prod.seller_id, "active")
                if status != "active":
                    raise ProductUnavailable(item.product_id)

                if prod.stock < item.quantity:
                    raise InsufficientStock(item.product_id)

            # Изпълнение на резервацията
            now = datetime.datetime.now(datetime.UTC)
            for item in items:
                prod = products_by_id[item.product_id]
                prod.stock -= item.quantity
                reservation = StockReservation(
                    order_id=order_id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                    status="reserved",
                    created_at=now,
                    updated_at=now,
                )
                self._reservation_repository.create(reservation)

            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def release_stock(self, order_id: int) -> None:
        """Идемпотентно освобождаване на резервирана наличност за поръчка.

        1. Заключва транзакционно order_id (pg_advisory_xact_lock) за защита от състезание reserve/release.
        2. Извлича и заключва записите за този order_id.
        3. Ако няма резервации или вече са освободени ('released') -> нищо не се променя.
        4. Ако има 'reserved' записи -> заключва продуктите, увеличава stock и обновява записа.
        5. Commit на транзакцията.
        """
        try:
            self._session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtext('catalog_reserve_' || :oid))"
                ),
                {"oid": str(order_id)},
            )
            self._reservation_repository.mark_order_released(order_id)
            existing = self._reservation_repository.get_by_order_id_for_update(order_id)
            if not existing:
                self._session.commit()
                return

            active_reservations = [r for r in existing if r.status == "reserved"]
            if not active_reservations:
                # Всички вече са освободени
                self._session.commit()
                return

            product_ids = sorted({r.product_id for r in active_reservations})
            locked_products = self._product_repository.get_by_ids_for_update(
                product_ids
            )
            products_by_id = {p.id: p for p in locked_products}

            now = datetime.datetime.now(datetime.UTC)
            for r in active_reservations:
                prod = products_by_id.get(r.product_id)
                if prod is not None:
                    prod.stock += r.quantity
                r.status = "released"
                r.updated_at = now

            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
