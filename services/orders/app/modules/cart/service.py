import logging
from typing import Any

from sqlalchemy.orm import Session

from app.modules.cart.repository import CartRepository
from app.modules.cart.schemas import CartItemResponse, CartResponse
from app.modules.catalog.client import CatalogClient
from app.modules.catalog.dependencies import resolve_catalog_client
from app.modules.catalog.service import ProductNotFound

logger = logging.getLogger(__name__)


class InsufficientStock(Exception):
    """Сигнализира, че заявеното количество надвишава текущата складова наличност."""


class CartService:
    """Услуга за бизнес логика и управление на сценариите с потребителски колички."""

    def __init__(
        self,
        cart_repository: CartRepository,
        product_repository: Any = None,
        session: Session | None = None,
        seller_status_repository: Any = None,
        user_repository: Any = None,
        users_client: Any = None,
        catalog_client: CatalogClient | None = None,
    ) -> None:
        self._cart_repository = cart_repository
        self._session = session
        self._catalog_client = resolve_catalog_client(catalog_client)
        self._product_repository = product_repository
        self._seller_status_repository = seller_status_repository
        from app.modules.accounts.repository import UserRepository

        self._user_repository = user_repository or (
            UserRepository(session) if session is not None else None
        )

    def get_cart(self, user_id: int) -> CartResponse:
        """Сценарий: извличане и изчисляване на текущата количка на потребител.

        Извлича продуктите групово чрез вътрешния API договор на Catalog.
        """
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

        # Стабилна подредба по product_id възходящо
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
        """Сценарий: задаване на точно количество за даден продукт в количката.

        Проверява активност и складова наличност единствено през Catalog договора.
        """
        try:
            if self._user_repository is not None:
                self._user_repository.lock_user(user_id)

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
            if self._session is not None:
                self._session.commit()
            return self.get_cart(user_id)
        except Exception:
            if self._session is not None:
                self._session.rollback()
            raise

    def remove_item(self, user_id: int, product_id: int) -> None:
        """Сценарий: премахване на продукт от количката на потребителя."""
        try:
            if self._user_repository is not None:
                self._user_repository.lock_user(user_id)

            self._cart_repository.delete_item(user_id=user_id, product_id=product_id)
            if self._session is not None:
                self._session.commit()
        except Exception:
            if self._session is not None:
                self._session.rollback()
            raise
