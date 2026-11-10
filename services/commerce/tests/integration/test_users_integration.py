"""Сквозни интеграционни проверки за интеграцията Commerce - Users (K6.1).

Проверява петте задължителни критерия:
1. Мигриран купувач влиза със стария си парол и вижда съществуващата си количка и поръчки;
2. Нов купувач се регистрира през публичния URL, използва количка и поръчва без запис в старата таблица users на Commerce;
3. Продавачът владее стоката и може да променя наличност и цена, а друг потребител получава отказ;
4. След блокиране в Users издаденият по-рано токен незабавно спира да дава достъп (403/401);
5. При спиране на Users защитените заявки в Commerce получават 503, а публичният преглед на каталога продължава да работи с 200.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

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
from app.modules.accounts.tokens import (
    InvalidToken,
    create_access_token,
    decode_access_token,
)
from app.modules.accounts.users_client import (
    RemoteUserInfo,
    UsersClient,
    UsersServiceUnavailableError,
)
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem
from tests.orders.helpers import ORDER_PAYLOAD

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
            except InvalidToken:
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

        elif (
            path.startswith("/users/") and path.endswith("/block") and method == "POST"
        ):
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
                json={
                    "detail": {"code": "USER_NOT_FOUND", "message": "User not found"}
                },
            )

        elif (
            path.startswith("/users/")
            and path.endswith("/unblock")
            and method == "POST"
        ):
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
                target.status = "active"
                target.token_version += 1
                return httpx.Response(
                    200,
                    json={
                        "id": target.id,
                        "action": "unblocked",
                        "status": "active",
                        "user_id": target.id,
                    },
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                404,
                json={
                    "detail": {"code": "USER_NOT_FOUND", "message": "User not found"}
                },
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
    login_res = client.post(
        "/auth/login", json={"email": email, "password": raw_password}
    )
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
    password = "SecurePassword123!"

    # 1. Регистрация на нов продавач през публичния URL
    seller_email = "real_marketplace_seller@example.com"
    reg_seller = client.post(
        "/users", json={"email": seller_email, "password": password}
    )
    assert reg_seller.status_code == 201, reg_seller.text
    assert reg_seller.json()["id"] > 0

    # 2. Вход на продавача и създаване на артикул през API
    seller_login = client.post(
        "/auth/login", json={"email": seller_email, "password": password}
    )
    assert seller_login.status_code == 200
    seller_token = seller_login.json()["access_token"]
    seller_headers = {"Authorization": f"Bearer {seller_token}"}

    prod_res = client.post(
        "/products",
        headers=seller_headers,
        json={
            "title": "Валиден артикул",
            "price_minor": 1500,
            "currency": "EUR",
            "stock": 20,
        },
    )
    assert prod_res.status_code == 201, prod_res.text
    valid_product_id = prod_res.json()["id"]

    # 3. Регистрация и вход на купувача през публичните ендпойнти
    buyer_email = "new_standalone_buyer@example.com"
    reg_buyer = client.post("/users", json={"email": buyer_email, "password": password})
    assert reg_buyer.status_code == 201, reg_buyer.text
    new_user_id = reg_buyer.json()["id"]

    # 4. В старата таблица users в Commerce НЯМА запис за продавача или купувача
    users_in_commerce = db_session.execute(
        text("SELECT id FROM users WHERE email IN (:b, :s)"),
        {"b": buyer_email, "s": seller_email},
    ).fetchall()
    assert users_in_commerce == [], (
        "В Commerce users не трябва да се създават shadow редове!"
    )

    buyer_login = client.post(
        "/auth/login", json={"email": buyer_email, "password": password}
    )
    assert buyer_login.status_code == 200
    buyer_token = buyer_login.json()["access_token"]
    buyer_headers = {"Authorization": f"Bearer {buyer_token}"}

    # 5. Отрицателен сценарий: продукт с неизвестен продавач (seller_id=999) в количката
    unknown_seller_prod = Product(
        title="Артикул с неизвестен продавач",
        price_minor=1500,
        stock=20,
        active=True,
        seller_id=999,
    )
    db_session.add(unknown_seller_prod)
    db_session.commit()

    add_unknown = client.put(
        f"/cart/items/{unknown_seller_prod.id}",
        json={"quantity": 3},
        headers=buyer_headers,
    )
    assert add_unknown.status_code == 200

    order_fail_headers = {
        **buyer_headers,
        "Idempotency-Key": str(uuid.uuid4()),
    }
    order_fail_res = client.post(
        "/orders", json=ORDER_PAYLOAD, headers=order_fail_headers
    )
    assert order_fail_res.status_code == 409
    assert order_fail_res.json()["detail"]["code"] == "CART_PRODUCT_UNAVAILABLE"
    assert order_fail_res.json()["detail"]["product_id"] == unknown_seller_prod.id

    # При отказ: поръчка не е създадена, наличността и количката са непроменени
    db_session.expire_all()
    orders_count = db_session.execute(
        text("SELECT count(*) FROM orders WHERE user_id = :uid"),
        {"uid": new_user_id},
    ).scalar()
    assert orders_count == 0
    refreshed_unknown = db_session.get(Product, unknown_seller_prod.id)
    assert refreshed_unknown is not None
    assert refreshed_unknown.stock == 20
    cart_check = client.get("/cart", headers=buyer_headers)
    assert len(cart_check.json()["items"]) == 1

    # Изчистваме невалидния продукт от количката
    client.delete(f"/cart/items/{unknown_seller_prod.id}", headers=buyer_headers)

    # 6. Положителен сценарий: покупка на продукт от валидния продавач
    add_valid = client.put(
        f"/cart/items/{valid_product_id}",
        json={"quantity": 3},
        headers=buyer_headers,
    )
    assert add_valid.status_code == 200

    idempotency_key = str(uuid.uuid4())
    order_success_headers = {
        **buyer_headers,
        "Idempotency-Key": idempotency_key,
    }
    order_res = client.post(
        "/orders", json=ORDER_PAYLOAD, headers=order_success_headers
    )
    assert order_res.status_code == 201, order_res.text
    order_data = order_res.json()
    assert order_data["total_minor"] == 4500

    # 7. Проверка за състоянието: поръчката съществува в БД, наличността е намалена, количката е празна
    db_session.expire_all()
    order_row = db_session.execute(
        text("SELECT user_id, customer_email FROM orders WHERE id = :order_id"),
        {"order_id": order_data["id"]},
    ).fetchone()
    assert order_row is not None
    assert order_row[0] == new_user_id
    assert order_row[1] == buyer_email

    refreshed_valid = db_session.get(Product, valid_product_id)
    assert refreshed_valid is not None
    assert refreshed_valid.stock == 17

    cart_empty_check = client.get("/cart", headers=buyer_headers)
    assert cart_empty_check.json()["items"] == []

    # 8. Повторен опит със същия Idempotency-Key връща съществуващата поръчка
    repeat_res = client.post(
        "/orders", json=ORDER_PAYLOAD, headers=order_success_headers
    )
    assert repeat_res.status_code == 201
    assert repeat_res.json()["id"] == order_data["id"]
    refreshed_after_repeat = db_session.get(Product, valid_product_id)
    assert refreshed_after_repeat is not None and refreshed_after_repeat.stock == 17

    # 9. Потребителите все още отсъстват от таблицата users в Commerce
    users_still_absent = db_session.execute(
        text("SELECT id FROM users WHERE email IN (:b, :s)"),
        {"b": buyer_email, "s": seller_email},
    ).fetchall()
    assert users_still_absent == []


def test_multi_seller_cart_with_one_blocked_seller_prevents_partial_order(
    db_session: Session,
) -> None:
    """Проверка за количка с двама продавачи: ако единият е блокиран, не се оформя дори част от поръчката."""
    password = "SecurePassword123!"

    # 1. Администратор за модерация
    admin_email = "admin_multi_mod@example.com"
    reg_admin = client.post("/users", json={"email": admin_email, "password": password})
    assert reg_admin.status_code == 201
    admin_id = reg_admin.json()["id"]
    admin_token = create_access_token(user_id=admin_id, role="admin", email=admin_email)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # 2. Продавач 1 (активен)
    s1_email = "seller_one@example.com"
    r_s1 = client.post("/users", json={"email": s1_email, "password": password})
    assert r_s1.status_code == 201
    token_s1 = client.post(
        "/auth/login", json={"email": s1_email, "password": password}
    ).json()["access_token"]
    h_s1 = {"Authorization": f"Bearer {token_s1}"}

    prod1 = client.post(
        "/products",
        headers=h_s1,
        json={
            "title": "Стока от Продавач 1",
            "price_minor": 1000,
            "stock": 10,
            "currency": "EUR",
        },
    ).json()

    # 3. Продавач 2 (ще бъде блокиран)
    s2_email = "seller_two_blocked@example.com"
    r_s2 = client.post("/users", json={"email": s2_email, "password": password})
    assert r_s2.status_code == 201
    s2_id = r_s2.json()["id"]
    token_s2 = client.post(
        "/auth/login", json={"email": s2_email, "password": password}
    ).json()["access_token"]
    h_s2 = {"Authorization": f"Bearer {token_s2}"}

    prod2 = client.post(
        "/products",
        headers=h_s2,
        json={
            "title": "Стока от Продавач 2",
            "price_minor": 2000,
            "stock": 10,
            "currency": "EUR",
        },
    ).json()

    # Администраторът блокира Продавач 2
    block_res = client.post(
        f"/users/{s2_id}/block",
        headers=admin_headers,
        json={"reason": "Terms violation"},
    )
    assert block_res.status_code == 200

    # 4. Купувачът добавя и двете стоки в количката
    buyer_email = "buyer_mixed_cart@example.com"
    r_b = client.post("/users", json={"email": buyer_email, "password": password})
    assert r_b.status_code == 201
    buyer_id = r_b.json()["id"]
    token_b = client.post(
        "/auth/login", json={"email": buyer_email, "password": password}
    ).json()["access_token"]
    h_b = {"Authorization": f"Bearer {token_b}"}

    client.put(f"/cart/items/{prod1['id']}", json={"quantity": 2}, headers=h_b)
    client.put(f"/cart/items/{prod2['id']}", json={"quantity": 3}, headers=h_b)

    cart_before = client.get("/cart", headers=h_b).json()
    assert len(cart_before["items"]) == 2

    # 5. Опит за поръчка: очакваме 409 заради блокирания продавач
    idemp_key = str(uuid.uuid4())
    order_headers = {**h_b, "Idempotency-Key": idemp_key}
    res_order = client.post("/orders", json=ORDER_PAYLOAD, headers=order_headers)
    assert res_order.status_code == 409
    assert res_order.json()["detail"]["code"] == "CART_PRODUCT_UNAVAILABLE"
    assert res_order.json()["detail"]["product_id"] == prod2["id"]

    # 6. Проверка, че НИКАКВА част от поръчката не е създадена и наличностите са непроменени
    db_session.expire_all()
    orders_cnt = db_session.execute(
        text("SELECT count(*) FROM orders WHERE user_id = :uid"),
        {"uid": buyer_id},
    ).scalar()
    assert orders_cnt == 0

    p1_db = db_session.get(Product, prod1["id"])
    p2_db = db_session.get(Product, prod2["id"])
    assert p1_db is not None and p1_db.stock == 10
    assert p2_db is not None and p2_db.stock == 10

    # Количката на купувача остава непроменена
    cart_after = client.get("/cart", headers=h_b).json()
    assert len(cart_after["items"]) == 2

    # 7. Повторение със същия Idempotency-Key при блокиран продавач все така отказва
    retry_order = client.post("/orders", json=ORDER_PAYLOAD, headers=order_headers)
    assert retry_order.status_code == 409
    assert retry_order.json()["detail"]["code"] == "CART_PRODUCT_UNAVAILABLE"

    # 8. Администраторът отблокира Продавач 2
    unblock_res = client.post(
        f"/users/{s2_id}/unblock",
        headers=admin_headers,
    )
    assert unblock_res.status_code == 200

    # 9. Опит за поръчка сега успява
    res_success = client.post(
        "/orders",
        json=ORDER_PAYLOAD,
        headers={**h_b, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert res_success.status_code == 201
    order_success = res_success.json()
    assert len(order_success["shipments"]) == 2

    db_session.expire_all()
    p1_after = db_session.get(Product, prod1["id"])
    p2_after = db_session.get(Product, prod2["id"])
    assert p1_after is not None and p1_after.stock == 8
    assert p2_after is not None and p2_after.stock == 7


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
    assert reg_seller.json()["id"] > 0

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
    admin_token = create_access_token(
        user_id=admin_id, role="admin", email="admin_blocker@example.com"
    )
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
        valid_token = create_access_token(
            user_id=10, role="customer", email="buyer@example.com"
        )
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

        order_post_res = client.post(
            "/orders",
            json=ORDER_PAYLOAD,
            headers={**auth_header, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert order_post_res.status_code == 503
        assert order_post_res.json()["detail"]["code"] == "USERS_SERVICE_UNAVAILABLE"

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
