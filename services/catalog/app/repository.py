"""Хранилища за достъп до данни в Catalog."""

import datetime
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    Category,
    Product,
    SellerInboxEvent,
    SellerStatus,
    StockReservation,
)


class CategoryRepository:
    """Хранилище за продуктови категории."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, category_id: int) -> Category | None:
        """Извлича категория по ID."""
        return self._session.get(Category, category_id)

    def get_by_name(self, name: str) -> Category | None:
        """Извлича категория по нормализирано име."""
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
    """Хранилище за продукти."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, product_id: int) -> Product | None:
        """Извлича продукт по неговото ID."""
        return self._session.get(Product, product_id)

    def get_active_by_id(self, product_id: int) -> Product | None:
        """Извлича активен продукт по ID, отчитайки статуса на продавача."""
        stmt = (
            select(Product)
            .outerjoin(SellerStatus, Product.seller_id == SellerStatus.seller_id)
            .where(
                Product.id == product_id,
                Product.active.is_(True),
                or_(
                    SellerStatus.seller_id.is_(None),
                    SellerStatus.status == "active",
                ),
            )
        )
        return self._session.scalars(stmt).one_or_none()

    def get_by_ids(self, product_ids: Sequence[int]) -> Sequence[Product]:
        """Извлича списък с продукти по IDs."""
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
        """Извлича списък с активни продукти, подредени по ID възходящо с пагинация."""
        stmt = (
            select(Product)
            .outerjoin(SellerStatus, Product.seller_id == SellerStatus.seller_id)
            .where(
                Product.active.is_(True),
                or_(
                    SellerStatus.seller_id.is_(None),
                    SellerStatus.status == "active",
                ),
            )
        )
        if category_id is not None:
            stmt = stmt.where(Product.category_id == category_id)
        stmt = stmt.order_by(Product.id.asc()).limit(limit).offset(offset)
        return self._session.scalars(stmt).all()

    def has_products_in_category(self, category_id: int) -> bool:
        """Проверява дали в дадена категория съществува поне един продукт."""
        stmt = select(Product.id).where(Product.category_id == category_id).limit(1)
        return self._session.scalars(stmt).first() is not None

    def list_by_seller(
        self, seller_id: int, limit: int, offset: int
    ) -> Sequence[Product]:
        """Извлича продукти на конкретен продавач (вкл. неактивни)."""
        stmt = (
            select(Product)
            .where(Product.seller_id == seller_id)
            .order_by(Product.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return self._session.scalars(stmt).all()

    def create(self, product: Product) -> Product:
        """Добавя продукт в текущата сесия и прави flush."""
        self._session.add(product)
        self._session.flush()
        return product


class SellerStatusRepository:
    """Хранилище за локалния статус на продавачи в Catalog."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, seller_id: int) -> SellerStatus | None:
        """Извлича статус на продавач по ID."""
        return self._session.get(SellerStatus, seller_id)

    def get_by_ids(self, seller_ids: Sequence[int]) -> dict[int, str]:
        """Извлича речник {seller_id: status} за подадените ID-та."""
        if not seller_ids:
            return {}
        stmt = select(SellerStatus).where(SellerStatus.seller_id.in_(set(seller_ids)))
        rows = self._session.scalars(stmt).all()
        return {r.seller_id: r.status for r in rows}

    def upsert_status(
        self,
        seller_id: int,
        status: str,
        updated_at: datetime.datetime | None = None,
    ) -> bool:
        """Вмъква или обновява локалния статус на продавач със защита от по-стари събития."""
        now = updated_at or datetime.datetime.now(datetime.UTC)
        stmt = (
            insert(SellerStatus)
            .values(seller_id=seller_id, status=status, updated_at=now)
            .on_conflict_do_update(
                index_elements=[SellerStatus.seller_id],
                set_={"status": status, "updated_at": now},
                where=(SellerStatus.updated_at <= now),
            )
            .returning(SellerStatus.seller_id)
        )
        res = self._session.execute(stmt).scalar_one_or_none()
        self._session.flush()
        return res is not None

    def is_inbox_event_processed(self, event_id: str) -> bool:
        """Проверява дали дадено събитие вече е обработено."""
        stmt = (
            select(SellerInboxEvent.event_id)
            .where(SellerInboxEvent.event_id == event_id)
            .limit(1)
        )
        return self._session.scalars(stmt).first() is not None

    def record_inbox_event_idempotent(
        self,
        event_id: str,
        seller_id: int,
        event_type: str,
        status: str,
    ) -> bool:
        """Записва събитие в inbox таблицата идемпотентно."""
        stmt = (
            insert(SellerInboxEvent)
            .values(
                event_id=event_id,
                seller_id=seller_id,
                event_type=event_type,
                status=status,
                received_at=datetime.datetime.now(datetime.UTC),
            )
            .on_conflict_do_nothing(index_elements=[SellerInboxEvent.event_id])
            .returning(SellerInboxEvent.event_id)
        )
        res = self._session.execute(stmt).scalar_one_or_none()
        self._session.flush()
        return res is not None


class ReservationRepository:
    """Хранилище за резервации на складови наличности."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_order_id(self, order_id: int) -> Sequence[StockReservation]:
        """Извлича всички резервации за дадена поръчка."""
        stmt = (
            select(StockReservation)
            .where(StockReservation.order_id == order_id)
            .order_by(StockReservation.product_id.asc())
        )
        return self._session.scalars(stmt).all()

    def get_by_order_id_for_update(self, order_id: int) -> Sequence[StockReservation]:
        """Извлича и заключва резервациите за поръчка."""
        stmt = (
            select(StockReservation)
            .where(StockReservation.order_id == order_id)
            .order_by(StockReservation.product_id.asc())
            .with_for_update()
        )
        return self._session.scalars(stmt).all()

    def create(self, reservation: StockReservation) -> StockReservation:
        """Добавя запис за резервация."""
        self._session.add(reservation)
        self._session.flush()
        return reservation
