"""Сквозни интеграционни проверки за интеграцията Commerce - Users (K6.1).

Проверява петте задължителни критерия:
1. Мигриран купувач влиза със стария си парол и вижда съществуващата си количка и поръчки;
2. Нов купувач се регистрира през публичния URL, използва количка и поръчва без запис в старата таблица users на Commerce;
3. Продавачът владее стоката и може да променя наличност и цена, а друг потребител получава отказ;
4. След блокиране в Users издаденият по-рано токен незабавно спира да дава достъп (403/401);
5. При спиране на Users защитените заявки в Commerce получават 503, а публичният преглед на каталога продължава да работи с 200.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash
from sqlalchemy import func, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.main import app
from app.modules.accounts.dependencies import get_users_client
from app.modules.accounts.models import User
from app.modules.accounts.service import _DUMMY_HASH
from app.modules.accounts.tokens import create_access_token, decode_access_token
from app.modules.accounts.users_client import (
    RemoteUserInfo,
    UsersClient,
    UsersServiceUnavailableError,
)
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem
from tests.orders.helpers import DEFAULT_SHIPPING_ADDRESS, ORDER_PAYLOAD

client = TestClient(app)
_hasher = PasswordHash.recommended()


@dataclass
class _MockUser:
    id: int
    email: str
    password_hash: str
    role: str = "customer"
    status: str = "active"
    token_version: int = 0
    display_name: str | None = None


class StandaloneUsersClient(UsersClient):
    """Модел на независим Users микросервис със собствена база от данни, изолирана от Commerce."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        super().__init__(base_url="http://test-users", internal_token="test-token")
        self.session_factory = session_factory
        self.users: dict[int, _MockUser] = {}
        self.email_map: dict[str, int] = {}
        self._next_id = 9000

    def get_user_status(self, user_id: int) -> RemoteUserInfo | None:
        if user_id in self.users:
            u = self.users[user_id]
            return RemoteUserInfo(
                id=u.id,
                email=u.email,
                role=u.role,
                status=u.status,
                token_version=u.token_version,
                display_name=u.display_name,
            )
        # Fallback към историческата таблица на Commerce за миграционния сценарий
        with self.session_factory() as session:
            db_u = session.get(User, user_id)
            if db_u is None:
                return None
            return RemoteUserInfo(
                id=db_u.id,
                email=db_u.email,
                role=db_u.role,
                status=db_u.status,
                token_version=db_u.token_version,
                display_name=db_u.display_name,
            )

    def forward_request(
        self,
        method: str,
        path: str,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        import json

        data = json.loads(content) if content else {}

        def _get_auth() -> tuple[int, Any, dict[str, str]]:
            auth = (headers or {}).get("authorization", "")
            if not auth.startswith("Bearer "):
                return (
                    401,
                    {
                        "detail": {
                            "code": "AUTHENTICATION_REQUIRED",
                            "message": "Authentication required",
                        }
                    },
                    {"WWW-Authenticate": "Bearer"},
                )
            token = auth[7:].strip()
            try:
                td = decode_access_token(
                    token,
                    public_key=settings.get_public_key(),
                    algorithm=settings.jwt_algorithm,
                    issuer=settings.jwt_issuer,
                    audience=settings.jwt_audience,
                )
            except Exception:
                return (
                    401,
                    {
                        "detail": {
                            "code": "INVALID_TOKEN",
                            "message": "Invalid or expired token",
                        }
                    },
                    {"WWW-Authenticate": "Bearer"},
                )

            u = self.users.get(td.user_id)
            if not u:
                with self.session_factory() as s:
                    db_u = s.get(User, td.user_id)
                    if db_u:
                        u = _MockUser(
                            id=db_u.id,
                            email=db_u.email,
                            password_hash=db_u.password_hash,
                            role=db_u.role,
                            status=db_u.status,
                            token_version=db_u.token_version,
                            display_name=db_u.display_name,
                        )
                        self.users[u.id] = u
                        self.email_map[u.email.lower()] = u.id

            if not u:
                return (
                    401,
                    {"detail": {"code": "USER_NOT_FOUND", "message": "User not found"}},
                    {"WWW-Authenticate": "Bearer"},
                )
            if u.token_version != td.token_version:
                return (
                    401,
                    {
                        "detail": {
                            "code": "INVALID_TOKEN",
                            "message": "Token has been revoked",
                        }
                    },
                    {"WWW-Authenticate": "Bearer"},
                )
            if u.status == "blocked":
                return (
                    403,
                    {
                        "detail": {
                            "code": "ACCOUNT_BLOCKED",
                            "message": "Account is blocked",
                        }
                    },
                    {},
                )
            if u.status != "active":
                return (
                    403,
                    {
                        "detail": {
                            "code": "ACCOUNT_INACTIVE",
                            "message": "Account is inactive",
                        }
                    },
                    {},
                )
            return 200, u, {}

        if path == "/users" and method == "POST":
            e = data["email"].strip().lower()
            if e in self.email_map:
                return httpx.Response(
                    409,
                    json={
                        "detail": {
                            "code": "EMAIL_ALREADY_EXISTS",
                            "message": "User with this email already exists",
                        }
                    },
                )
            self._next_id += 1
            u = _MockUser(
                id=self._next_id,
                email=data["email"].strip(),
                password_hash=_hasher.hash(data["password"]),
            )
            self.users[u.id] = u
            self.email_map[e] = u.id
            return httpx.Response(
                201,
                json={
                    "id": u.id,
                    "email": u.email,
                    "role": u.role,
                    "display_name": u.display_name,
                },
                headers={"content-type": "application/json"},
            )

        elif path == "/auth/login" and method == "POST":
            e = data["email"].strip().lower()
            uid = self.email_map.get(e)
            u = self.users.get(uid) if uid else None
            if not u:
                with self.session_factory() as s:
                    db_u = s.query(User).filter(func.lower(User.email) == e).first()
                    if db_u:
                        u = _MockUser(
                            id=db_u.id,
                            email=db_u.email,
                            password_hash=db_u.password_hash,
                            role=db_u.role,
                            status=db_u.status,
                            token_version=db_u.token_version,
                            display_name=db_u.display_name,
                        )
                        self.users[u.id] = u
                        self.email_map[e] = u.id

            if not u or not _hasher.verify(data["password"], u.password_hash):
                _hasher.verify(data["password"], _DUMMY_HASH)
                return httpx.Response(
                    401,
                    json={
                        "detail": {
                            "code": "INVALID_CREDENTIALS",
                            "message": "Invalid email or password",
                        }
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )

            if u.status == "blocked":
                return httpx.Response(
                    403,
                    json={
                        "detail": {
                            "code": "ACCOUNT_BLOCKED",
                            "message": "Account is blocked",
                        }
                    },
                )

            token = create_access_token(
                user_id=u.id,
                role=u.role,
                email=u.email,
                token_version=u.token_version,
            )
            return httpx.Response(
                200,
                json={"access_token": token, "token_type": "bearer"},
                headers={"content-type": "application/json"},
            )

        elif path == "/users/me" and method == "GET":
            c, u_or_err, hdrs = _get_auth()
            if c != 200:
                return httpx.Response(c, json=u_or_err, headers=hdrs)
            return httpx.Response(
                200,
                json={
                    "id": u_or_err.id,
                    "email": u_or_err.email,
                    "role": u_or_err.role,
                    "display_name": u_or_err.display_name,
                },
                headers={"content-type": "application/json"},
            )

        elif path.startswith("/users/") and path.endswith("/block") and method == "POST":
            c, u_or_err, hdrs = _get_auth()
            if c != 200:
                return httpx.Response(c, json=u_or_err, headers=hdrs)
            target_id = int(path.split("/")[2])
            target = self.users.get(target_id)
            if not target:
                with self.session_factory() as s:
                    db_u = s.get(User, target_id)
                    if db_u:
                        target = _MockUser(
                            id=db_u.id,
                            email=db_u.email,
                            password_hash=db_u.password_hash,
                            role=db_u.role,
                            status=db_u.status,
                            token_version=db_u.token_version,
                            display_name=db_u.display_name,
                        )
                        self.users[target.id] = target
                        self.email_map[target.email.lower()] = target.id
            if target:
                target.status = "blocked"
                target.token_version += 1
                return httpx.Response(
                    200,
                    json={
                        "id": target.id,
                        "action": "blocked",
                        "status": "blocked",
                        "user_id": target.id,
                    },
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                404,
                json={"detail": {"code": "USER_NOT_FOUND", "message": "User not found"}},
            )

        return httpx.Response(404)


@pytest.fixture(autouse=True)
def setup_standalone_users(
    session_factory: sessionmaker[Session],
) -> Iterator[StandaloneUsersClient]:
    """Активира независим Users клиент за интеграционните тестове."""
    client_instance = StandaloneUsersClient(session_factory)
    old_override = app.dependency_overrides.get(get_users_client)
    app.dependency_overrides[get_users_client] = lambda: client_instance
    yield client_instance
    if old_override is not None:
        app.dependency_overrides[get_users_client] = old_override
    else:
        app.dependency_overrides.pop(get_users_client, None)


def test_migrated_buyer_logs_in_and_sees_existing_cart_and_orders(
    db_session: Session,
) -> None:
    """1. Мигриран купувач влиза с предишната си парола и вижда своята количка и поръчки."""
    email = "migrated_buyer@example.com"
    raw_password = "ExistingPassword123!"
    pw_hash = _hasher.hash(raw_password)

    # Потребителят съществува в историческата таблица на Commerce
    hist_user = User(
        email=email,
        password_hash=pw_hash,
        role="customer",
        status="active",
        token_version=0,
    )
    db_session.add(hist_user)
    db_session.flush()

    # Създаваме продукт, позиция в количката и историческа поръчка
    product = Product(
        title="Мигриран продукт",
        price_minor=2500,
        stock=10,
        active=True,
        seller_id=1,
    )
    db_session.add(product)
    db_session.flush()

    cart_item = CartItem(user_id=hist_user.id, product_id=product.id, quantity=2)
    order = Order(
        user_id=hist_user.id,
        status="paid",
        currency="EUR",
        total_minor=5000,
        customer_email=email,
        idempotency_key=str(uuid.uuid4()),
        shipping_recipient_name="Иван Иванов",
        shipping_phone="+359888123456",
        shipping_country_code="BG",
        shipping_city="София",
        shipping_postal_code="1000",
        shipping_address_line1="бул. Витоша 1",
    )
    db_session.add_all([cart_item, order])
    db_session.flush()

    order_item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        seller_id=1,
        product_title=product.title,
        unit_price_minor=2500,
        quantity=2,
        subtotal_minor=5000,
    )
    db_session.add(order_item)
    db_session.commit()

    # Купувачът влиза успешно през публичния вход с предишната си парола
    login_res = client.post("/auth/login", json={"email": email, "password": raw_password})
    assert login_res.status_code == 200, login_res.text
    token = login_res.json()["access_token"]
    auth_header = {"Authorization": f"Bearer {token}"}

    # Вижда съществуващата си количка
    cart_res = client.get("/cart", headers=auth_header)
    assert cart_res.status_code == 200
    cart_items = cart_res.json()["items"]
    assert len(cart_items) == 1
    assert cart_items[0]["product_id"] == product.id
    assert cart_items[0]["quantity"] == 2

    # Вижда съществуващата си поръчка
    orders_res = client.get("/orders", headers=auth_header)
    assert orders_res.status_code == 200
    orders_data = orders_res.json()["items"]
    assert len(orders_data) >= 1
    assert orders_data[0]["id"] == order.id
    assert orders_data[0]["total_minor"] == 5000


def test_new_buyer_registers_via_public_url_and_places_order_without_commerce_user_row(
    db_session: Session,
) -> None:
    """2. Нов купувач се регистрира, използва количка и оформя поръчка без запис в Commerce users."""
    email = "new_standalone_buyer@example.com"
    password = "NewBuyerSecurePassword123!"

    # 1. Регистрация през публичния URL
    reg_res = client.post("/users", json={"email": email, "password": password})
    assert reg_res.status_code == 201, reg_res.text
    new_user_id = reg_res.json()["id"]

    # 2. Уверяваме се, че в старата таблица users в Commerce НЯМА запис за този потребител
    user_in_commerce = db_session.execute(
        text("SELECT id FROM users WHERE email = :email"),
        {"email": email},
    ).scalar_one_or_none()
    assert user_in_commerce is None, "В Commerce users не трябва да се създава shadow ред!"

    # 3. Вход през публичния вход
    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    auth_header = {"Authorization": f"Bearer {token}"}

    # 4. Продукт в каталога
    product = Product(
        title="Нов тестов артикул",
        price_minor=1500,
        stock=20,
        active=True,
        seller_id=999,
    )
    db_session.add(product)
    db_session.commit()

    # 5. Добавяне в количката
    add_cart = client.put(
        f"/cart/items/{product.id}",
        json={"quantity": 3},
        headers=auth_header,
    )
    assert add_cart.status_code == 200, add_cart.text

    # 6. Оформяне на поръчка
    order_headers = {
        **auth_header,
        "Idempotency-Key": str(uuid.uuid4()),
    }
    order_res = client.post("/orders", json=ORDER_PAYLOAD, headers=order_headers)
    assert order_res.status_code == 201, order_res.text
    order_data = order_res.json()
    assert order_data["total_minor"] == 4500

    # 7. Проверка в БД: поръчката съществува с правилния user_id и customer_email
    order_row = db_session.execute(
        text("SELECT user_id, customer_email FROM orders WHERE id = :order_id"),
        {"order_id": order_data["id"]},
    ).fetchone()
    assert order_row is not None
    assert order_row[0] == new_user_id
    assert order_row[1] == email

    # 8. Отново проверяваме, че в Commerce users все още няма запис
    user_still_absent = db_session.execute(
        text("SELECT id FROM users WHERE email = :email"),
        {"email": email},
    ).scalar_one_or_none()
    assert user_still_absent is None


def test_seller_owns_product_and_can_update_while_non_owner_is_denied(
    db_session: Session,
) -> None:
    """3. Продавачът владее стоката и може да променя остатък и цена; друг потребител получава отказ."""
    seller_email = "seller_owner@example.com"
    seller_pass = "SellerPassword12345!"
    reg_seller = client.post(
        "/users", json={"email": seller_email, "password": seller_pass}
    )
    assert reg_seller.status_code == 201
    seller_id = reg_seller.json()["id"]

    login_seller = client.post(
        "/auth/login", json={"email": seller_email, "password": seller_pass}
    )
    seller_token = login_seller.json()["access_token"]
    seller_header = {"Authorization": f"Bearer {seller_token}"}

    # Създаваме продукт на този продавач
    create_prod = client.post(
        "/products",
        json={
            "title": "Собствен артикул",
            "price_minor": 1000,
            "currency": "EUR",
            "stock": 10,
        },
        headers=seller_header,
    )
    assert create_prod.status_code == 201, create_prod.text
    product_id = create_prod.json()["id"]

    # Продавачът успешно променя наличността и цената
    update_res = client.patch(
        f"/products/{product_id}",
        json={"price_minor": 1800, "stock": 25},
        headers=seller_header,
    )
    assert update_res.status_code == 200
    assert update_res.json()["price_minor"] == 1800
    assert update_res.json()["stock"] == 25

    # Друг потребител (купувач или друг продавач)
    other_email = "other_intruder@example.com"
    other_pass = "IntruderPassword123!"
    client.post("/users", json={"email": other_email, "password": other_pass})
    login_other = client.post(
        "/auth/login", json={"email": other_email, "password": other_pass}
    )
    other_token = login_other.json()["access_token"]
    other_header = {"Authorization": f"Bearer {other_token}"}

    # Опитът на друг потребител да редактира чуждия продукт е отказан (404/403)
    unauth_patch = client.patch(
        f"/products/{product_id}",
        json={"price_minor": 100, "stock": 0},
        headers=other_header,
    )
    assert unauth_patch.status_code in (403, 404)

    # Проверяваме, че цената и остатъкът не са променени
    get_res = client.get(f"/products/{product_id}")
    assert get_res.status_code == 200
    assert get_res.json()["price_minor"] == 1800
    assert get_res.json()["stock"] == 25


def test_blocked_user_token_immediately_invalidated(
    db_session: Session,
    setup_standalone_users: StandaloneUsersClient,
) -> None:
    """4. След блокиране в Users издаденият по-рано токен спира да дава достъп (403/401) в Commerce."""
    admin_id = 9999
    setup_standalone_users.users[admin_id] = _MockUser(
        id=admin_id,
        email="admin_blocker@example.com",
        password_hash=_hasher.hash("AdminPassword123!"),
        role="admin",
        status="active",
    )
    setup_standalone_users.email_map["admin_blocker@example.com"] = admin_id
    admin_token = create_access_token(user_id=admin_id, role="admin", email="admin_blocker@example.com")
    admin_header = {"Authorization": f"Bearer {admin_token}"}

    user_email = "target_suspend_user@example.com"
    user_pass = "ActiveSuspendedPassword123!"
    reg_res = client.post("/users", json={"email": user_email, "password": user_pass})
    assert reg_res.status_code == 201
    user_id = reg_res.json()["id"]

    login_res = client.post(
        "/auth/login", json={"email": user_email, "password": user_pass}
    )
    assert login_res.status_code == 200
    user_token = login_res.json()["access_token"]
    user_header = {"Authorization": f"Bearer {user_token}"}

    # Преди блока защитените ресурси отговарят успешно
    assert client.get("/users/me", headers=user_header).status_code == 200
    assert client.get("/cart", headers=user_header).status_code == 200

    # Блокираме потребителя през админ маршрута в Users
    block_res = client.post(
        f"/users/{user_id}/block",
        json={"reason": "Suspicious fraudulent activity"},
        headers=admin_header,
    )
    assert block_res.status_code == 200

    # Същият токен незабавно спира да дава достъп до Commerce ресурси (401 или 403)
    blocked_me = client.get("/users/me", headers=user_header)
    assert blocked_me.status_code in (401, 403)

    blocked_cart = client.get("/cart", headers=user_header)
    assert blocked_cart.status_code in (401, 403)

    blocked_orders = client.get("/orders", headers=user_header)
    assert blocked_orders.status_code in (401, 403)


def test_users_service_downtime_protected_fails_with_503_while_catalog_read_returns_200(
    db_session: Session,
) -> None:
    """5. При спиране на Users защитените заявки получават 503, а публичният каталог връща 200."""
    class UnavailableUsersClient(UsersClient):
        def get_user_status(self, user_id: int):
            raise UsersServiceUnavailableError("Users service down")

        def forward_request(self, *args, **kwargs):
            raise UsersServiceUnavailableError("Users service down")

    old_override = app.dependency_overrides.get(get_users_client)
    app.dependency_overrides[get_users_client] = lambda: UnavailableUsersClient()

    try:
        valid_token = create_access_token(user_id=10, role="customer", email="buyer@example.com")
        auth_header = {"Authorization": f"Bearer {valid_token}"}

        # Създаваме продукт за публичен преглед
        product = Product(
            title="Публичен продукт",
            price_minor=3000,
            stock=5,
            active=True,
            seller_id=1,
        )
        db_session.add(product)
        db_session.commit()

        # 1. Защитена заявка в Commerce (напр. количка) връща 503 Service Unavailable
        protected_res = client.get("/cart", headers=auth_header)
        assert protected_res.status_code == 503
        assert protected_res.json()["detail"]["code"] == "USERS_SERVICE_UNAVAILABLE"

        # 2. Защитена заявка за поръчки връща 503
        orders_res = client.get("/orders", headers=auth_header)
        assert orders_res.status_code == 503
        assert orders_res.json()["detail"]["code"] == "USERS_SERVICE_UNAVAILABLE"

        # 3. Публичният преглед на каталога НЕ зависи от Users и продължава да работи успешно (200)
        catalog_list = client.get("/products")
        assert catalog_list.status_code == 200
        items = catalog_list.json()["items"]
        assert any(i["id"] == product.id for i in items)

        catalog_detail = client.get(f"/products/{product.id}")
        assert catalog_detail.status_code == 200
        assert catalog_detail.json()["title"] == "Публичен продукт"
    finally:
        if old_override is not None:
            app.dependency_overrides[get_users_client] = old_override
        else:
            app.dependency_overrides.pop(get_users_client, None)
