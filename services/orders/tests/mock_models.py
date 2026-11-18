import datetime
from typing import Any


class MockBase:
    """Базов клас за тестови mock обекти."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class User(MockBase):
    id: int | None = None
    email: str = ""
    password_hash: str = ""
    role: str = "customer"
    status: str = "active"
    display_name: str | None = None
    token_version: int = 0

    def __init__(
        self,
        id: int | None = None,
        email: str = "",
        password_hash: str = "",
        role: str = "customer",
        status: str = "active",
        display_name: str | None = None,
        token_version: int = 0,
        **kwargs: Any,
    ) -> None:
        self.id = id if (id is not None and id != 0) else None
        self.email = email
        self.password_hash = password_hash
        self.role = role
        self.status = status
        self.display_name = display_name
        self.token_version = token_version
        super().__init__(**kwargs)


class PasswordResetToken(MockBase):
    id: int = 0
    user_id: int = 0
    token_hash: str = ""
    expires_at: datetime.datetime | None = None
    used_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None


class AccountModerationLog(MockBase):
    id: int = 0
    target_user_id: int = 0
    actor_admin_id: int = 0
    action: str = ""
    reason: str | None = None
    created_at: datetime.datetime | None = None


class Category(MockBase):
    id: int = 0
    name: str = ""

    def __init__(self, id: int = 0, name: str = "", **kwargs: Any) -> None:
        self.id = id
        self.name = name
        super().__init__(**kwargs)


class Product(MockBase):
    id: int | None = None
    seller_id: int = 1
    category_id: int | None = None
    title: str = ""
    price_minor: int = 0
    currency: str = "EUR"
    stock: int = 0
    active: bool = True
    seller_active: bool = True

    def __init__(
        self,
        id: int | None = None,
        seller_id: int = 1,
        category_id: int | None = None,
        title: str = "",
        price_minor: int = 0,
        currency: str = "EUR",
        stock: int = 0,
        active: bool = True,
        seller_active: bool = True,
        **kwargs: Any,
    ) -> None:
        self.id = id if (id is not None and id != 0) else None
        self.seller_id = seller_id
        self.category_id = category_id
        self.title = title
        self.price_minor = price_minor
        self.currency = currency
        self.stock = stock
        self.active = active
        self.seller_active = seller_active
        super().__init__(**kwargs)


class SellerStatus(MockBase):
    seller_id: int = 0
    status: str = "active"
    updated_at: datetime.datetime | None = None


class CartItem(MockBase):
    user_id: int = 0
    product_id: int = 0
    quantity: int = 1

    def __init__(
        self,
        user_id: int = 0,
        product_id: int = 0,
        quantity: int = 1,
        **kwargs: Any,
    ) -> None:
        self.user_id = user_id
        self.product_id = product_id
        self.quantity = quantity
        super().__init__(**kwargs)
