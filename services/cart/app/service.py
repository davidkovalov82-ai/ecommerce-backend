"""Бизнес логика за управление на количката в Cart услугата."""

import logging

from sqlalchemy.orm import Session

from app.catalog_client import CatalogClient
from app.repository import CartRepository
from app.schemas import CartItemResponse, CartResponse

logger = logging.getLogger(__name__)


class ProductNotFound(Exception):
    """Сигнализира, че продуктът не съществува в каталога или е деактивиран."""


class InsufficientStock(Exception):
    """Сигнализира, че заявеното количество надвишава текущата складова наличност."""


class CartService:
    """Услуга за управление на потребителската количка.

    Cart управлява единствено състава и количествата.
    Цените, наличността и видимостта се извличат от Catalog.
    Добавянето в количката НЕ резервира складова наличност.
    """

    def __init__(
        self,
        session: Session,
        cart_repository: CartRepository,
        catalog_client: CatalogClient,
    ) -> None:
        self._session = session
        self._cart_repository = cart_repository
        self._catalog_client = catalog_client

    def get_cart(self, user_id: int) -> CartResponse:
        """Извлича количката за даден потребител и я обогатява с данни от Catalog."""
        cart_items = self._cart_repository.list_by_user_id(user_id)
        if not cart_items:
            return CartResponse(items=[], total_minor=0, currency="EUR")

        product_ids = [item.product_id for item in cart_items]
        products = self._catalog_client.get_products_batch(product_ids)
        products_by_id = {product.id: product for product in products}

        item_responses: list[CartItemResponse] = []
        total_minor = 0

        for item in cart_items:
            product = products_by_id.get(item.product_id)
            if product is None:
                continue

            subtotal_minor = product.price_minor * item.quantity
            total_minor += subtotal_minor
            available = bool(
                product.active
                and getattr(product, "seller_active", True)
                and product.stock >= item.quantity
            )

            item_responses.append(
                CartItemResponse(
                    product_id=item.product_id,
                    title=product.title,
                    quantity=item.quantity,
                    unit_price_minor=product.price_minor,
                    subtotal_minor=subtotal_minor,
                    available=available,
                )
            )

        item_responses.sort(key=lambda x: x.product_id)

        return CartResponse(
            items=item_responses,
            total_minor=total_minor,
            currency="EUR",
        )

    def set_item_quantity(
        self,
        user_id: int,
        product_id: int,
        quantity: int,
    ) -> CartResponse:
        """Задава количество за даден продукт в количката.

        Проверява валидност и наличност през Catalog, без да резервира остатък.
        """
        try:
            product = self._catalog_client.get_product(product_id)
            if (
                product is None
                or not product.active
                or not getattr(product, "seller_active", True)
            ):
                raise ProductNotFound(f"Product with id {product_id} not found")

            if quantity > product.stock:
                raise InsufficientStock(
                    f"Requested quantity {quantity} exceeds available stock {product.stock}"
                )

            self._cart_repository.upsert_item(
                user_id=user_id,
                product_id=product_id,
                quantity=quantity,
            )
            self._session.commit()
            return self.get_cart(user_id)
        except Exception:
            self._session.rollback()
            raise

    def remove_item(self, user_id: int, product_id: int) -> None:
        """Премахва продукт от количката на потребителя."""
        try:
            self._cart_repository.delete_item(user_id=user_id, product_id=product_id)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def clear_purchased(self, user_id: int, product_ids: list[int]) -> list[int]:
        """Премахва единствено успешно закупените позиции за дадена поръчка.

        Всички останали промени в количката по време на плащането се запазват.
        """
        try:
            self._cart_repository.delete_items(user_id=user_id, product_ids=product_ids)
            self._session.commit()
            return product_ids
        except Exception:
            self._session.rollback()
            raise
