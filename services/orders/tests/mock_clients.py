"""Локални mock клиенти за микросервизните договори на Users, Catalog и Cart.

Използват се за изолирани тестове на Orders без вдигане на реални отдалечени HTTP сервизи.
"""

from typing import Any, ClassVar

from app.modules.accounts.users_client import RemoteUserInfo, UsersClient
from app.modules.cart.client import CartClient, RemoteCartInfo, RemoteCartItem
from app.modules.catalog.client import (
    CatalogClient,
    CatalogInsufficientStockError,
    CatalogProductUnavailableError,
    RemoteProductInfo,
)


class LocalTestUsersClient(UsersClient):
    """Тестов клиент, който управлява потребителски статус локално в паметта."""

    _users: ClassVar[dict[int, RemoteUserInfo]] = {}

    def __init__(self) -> None:
        super().__init__(base_url="http://test-users", internal_token="test-token")

    def set_mock_user(
        self,
        user_id: int,
        email: str,
        role: str = "customer",
        status: str = "active",
        token_version: int = 0,
        display_name: str | None = None,
    ) -> RemoteUserInfo:
        u = RemoteUserInfo(
            id=user_id,
            email=email,
            role=role,
            status=status,
            token_version=token_version,
            display_name=display_name,
        )
        self._users[user_id] = u
        return u

    def get_user_status(self, user_id: int) -> RemoteUserInfo | None:
        if user_id in self._users:
            return self._users[user_id]
        return RemoteUserInfo(
            id=user_id,
            email=f"user{user_id}@example.com",
            role="customer",
            status="active",
            token_version=0,
            display_name=None,
        )


class LocalTestCatalogClient(CatalogClient):
    """Тестов клиент за Catalog, управляващ стоките в паметта."""

    _products: ClassVar[dict[int, RemoteProductInfo]] = {}
    _reservations: ClassVar[dict[int, list[dict[str, Any]]]] = {}
    _released_orders: ClassVar[set[int]] = set()

    def __init__(self) -> None:
        super().__init__(base_url="http://test-catalog", internal_token="test-token")

    def set_mock_product(
        self,
        product_id: int,
        title: str = "Test Product",
        price_minor: int = 1000,
        currency: str = "EUR",
        stock: int = 10,
        active: bool = True,
        seller_id: int = 1,
        seller_active: bool = True,
        category_id: int | None = None,
    ) -> RemoteProductInfo:
        prod = RemoteProductInfo(
            id=product_id,
            seller_id=seller_id,
            category_id=category_id,
            title=title,
            price_minor=price_minor,
            currency=currency,
            stock=stock,
            active=active,
            seller_active=seller_active,
        )
        self._products[product_id] = prod
        return prod

    def update_product_field(self, product_id: int, key: str, value: Any) -> None:
        if product_id in self._products:
            p = self._products[product_id]
            data = {
                "id": p.id,
                "seller_id": p.seller_id,
                "category_id": p.category_id,
                "title": p.title,
                "price_minor": p.price_minor,
                "currency": p.currency,
                "stock": p.stock,
                "active": p.active,
                "seller_active": p.seller_active,
            }
            data[key] = value
            self._products[product_id] = RemoteProductInfo(**data)

    def get_product(self, product_id: int) -> RemoteProductInfo | None:
        return self._products.get(product_id)

    def get_products_batch(self, product_ids: list[int]) -> list[RemoteProductInfo]:
        return [self._products[pid] for pid in product_ids if pid in self._products]

    def reserve_stock(self, order_id: int, items: list[dict[str, int]]) -> None:
        if order_id in self._released_orders:
            raise CatalogProductUnavailableError(
                0, f"Reservation for order {order_id} has already been released"
            )
        if order_id in self._reservations:
            return

        for item in items:
            pid = item["product_id"]
            qty = item["quantity"]
            if pid not in self._products:
                raise CatalogProductUnavailableError(pid)
            p = self._products[pid]
            if not p.active or not getattr(p, "seller_active", True):
                raise CatalogProductUnavailableError(pid)
            if p.stock < qty:
                raise CatalogInsufficientStockError(pid)

        for item in items:
            pid = item["product_id"]
            qty = item["quantity"]
            p = self._products[pid]
            self._products[pid] = RemoteProductInfo(
                id=p.id,
                seller_id=p.seller_id,
                category_id=p.category_id,
                title=p.title,
                price_minor=p.price_minor,
                currency=p.currency,
                stock=p.stock - qty,
                active=p.active,
                seller_active=p.seller_active,
            )

        self._reservations[order_id] = [dict(item) for item in items]

    def release_stock(self, order_id: int) -> None:
        if order_id in self._released_orders:
            return
        self._released_orders.add(order_id)
        reserved_items = self._reservations.pop(order_id, None)
        if reserved_items is not None:
            for item in reserved_items:
                pid = item["product_id"]
                qty = item["quantity"]
                if pid in self._products:
                    p = self._products[pid]
                    self._products[pid] = RemoteProductInfo(
                        id=p.id,
                        seller_id=p.seller_id,
                        category_id=p.category_id,
                        title=p.title,
                        price_minor=p.price_minor,
                        currency=p.currency,
                        stock=p.stock + qty,
                        active=p.active,
                        seller_active=p.seller_active,
                    )


class LocalTestCartClient(CartClient):
    """Тестов клиент за Cart, управляващ количката в паметта с версии и идемпотентно изчистване."""

    _carts: ClassVar[dict[int, dict[int, dict[str, Any]]]] = {}
    _cleared_orders: ClassVar[set[int]] = set()

    def __init__(self, catalog_client: LocalTestCatalogClient) -> None:
        super().__init__(base_url="http://test-cart", internal_token="test-token")
        self.catalog_client = catalog_client

    def set_item(
        self, user_id: int, product_id: int, quantity: int, version: int = 1
    ) -> None:
        if user_id not in self._carts:
            self._carts[user_id] = {}
        self._carts[user_id][product_id] = {
            "quantity": quantity,
            "version": version,
        }

    def get_cart(self, user_id: int) -> RemoteCartInfo:
        user_items = self._carts.get(user_id, {})
        if not user_items:
            return RemoteCartInfo(items=[], total_minor=0, currency="EUR")

        items: list[RemoteCartItem] = []
        total = 0
        for pid in sorted(user_items.keys()):
            cdata = user_items[pid]
            p = self.catalog_client.get_product(pid)
            if p is None:
                continue
            available = bool(
                p.active
                and getattr(p, "seller_active", True)
                and p.stock >= cdata["quantity"]
            )
            subtotal = p.price_minor * cdata["quantity"]
            total += subtotal
            items.append(
                RemoteCartItem(
                    product_id=pid,
                    title=p.title,
                    quantity=cdata["quantity"],
                    unit_price_minor=p.price_minor,
                    subtotal_minor=subtotal,
                    available=available,
                    version=cdata.get("version", 1),
                )
            )
        return RemoteCartInfo(items=items, total_minor=total, currency="EUR")

    def clear_purchased(
        self,
        user_id: int,
        items: list[dict[str, Any]] | None = None,
        product_ids: list[int] | None = None,
        order_id: int | None = None,
    ) -> dict[str, Any]:
        if order_id is not None:
            if order_id in self._cleared_orders:
                return {
                    "status": "cleared",
                    "user_id": user_id,
                    "cleared_product_ids": [],
                }
            self._cleared_orders.add(order_id)

        user_cart = self._carts.get(user_id, {})
        cleared_ids: list[int] = []

        if items is not None:
            for it in items:
                pid = it["product_id"] if isinstance(it, dict) else it.product_id
                ver = it["version"] if isinstance(it, dict) else it.version
                if pid in user_cart and (
                    user_cart[pid].get("version", 1) == ver or ver is None
                ):
                    del user_cart[pid]
                    cleared_ids.append(pid)
        elif product_ids is not None:
            for pid in product_ids:
                if pid in user_cart:
                    del user_cart[pid]
                    cleared_ids.append(pid)

        return {
            "status": "cleared",
            "user_id": user_id,
            "cleared_product_ids": cleared_ids,
        }


shared_users_client = LocalTestUsersClient()
shared_catalog_client = LocalTestCatalogClient()
shared_cart_client = LocalTestCartClient(shared_catalog_client)
