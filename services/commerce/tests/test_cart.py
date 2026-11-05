import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.dependencies import get_db_session
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Product
from tests.conftest import TestingSessionLocal

client = TestClient(app)


def _create_user_and_token(
    db_session: Session,
    email: str = "customer@example.com",
    role: str = "customer",
) -> tuple[User, str]:
    """Помощна функция за създаване на тестов потребител и валиден Bearer токен."""
    user = User(
        email=email,
        password_hash="argon2_test_hash",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(user_id=user.id, secret=settings.jwt_secret)
    return user, token


def _create_product(
    db_session: Session,
    title: str = "Test Product",
    price_minor: int = 1000,
    stock: int = 10,
    active: bool = True,
) -> Product:
    """Помощна функция за създаване на тестов продукт."""
    product = Product(
        title=title,
        price_minor=price_minor,
        currency="EUR",
        stock=stock,
        active=active,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


# 1. 401 при липсващ или невалиден токен
def test_cart_endpoints_require_authentication() -> None:
    """Критерий: Всички операции с количката изискват валидна автентикация (401)."""
    # 1. GET /cart без токен
    res_get = client.get("/cart")
    assert res_get.status_code == 401
    assert res_get.headers.get("WWW-Authenticate") == "Bearer"
    assert res_get.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"

    # 2. PUT /cart/items/1 без токен
    res_put = client.put("/cart/items/1", json={"quantity": 1})
    assert res_put.status_code == 401
    assert res_put.headers.get("WWW-Authenticate") == "Bearer"
    assert res_put.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"

    # 3. DELETE /cart/items/1 без токен
    res_del = client.delete("/cart/items/1")
    assert res_del.status_code == 401
    assert res_del.headers.get("WWW-Authenticate") == "Bearer"
    assert res_del.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"

    # 4. Невалиден токен
    bad_headers = {"Authorization": "Bearer invalid_expired_or_corrupted_token"}
    for res in [
        client.get("/cart", headers=bad_headers),
        client.put("/cart/items/1", json={"quantity": 1}, headers=bad_headers),
        client.delete("/cart/items/1", headers=bad_headers),
    ]:
        assert res.status_code == 401
        assert res.headers.get("WWW-Authenticate") == "Bearer"
        assert res.json()["detail"]["code"] == "INVALID_TOKEN"


# 2. Празна количка
def test_empty_cart_returns_expected_structure(db_session: Session) -> None:
    """Критерий: Празната количка връща 200 с items=[], total_minor=0, currency='EUR'."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get("/cart", headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "total_minor": 0,
        "currency": "EUR",
    }


# 3. PUT добавя нова позиция, задава точно количество и е идемпотентен
def test_put_cart_item_adds_and_updates_idempotently(db_session: Session) -> None:
    """Критерий: PUT добавя позиция, задава точно количество и повторен PUT връща същия резултат."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    product = _create_product(db_session, price_minor=1500, stock=10)

    # 1. Добавяне на позиция с количество 3
    res1 = client.put(
        f"/cart/items/{product.id}", json={"quantity": 3}, headers=headers
    )
    assert res1.status_code == 200
    expected_cart_3 = {
        "items": [
            {
                "product_id": product.id,
                "title": product.title,
                "quantity": 3,
                "unit_price_minor": 1500,
                "subtotal_minor": 4500,
                "available": True,
            }
        ],
        "total_minor": 4500,
        "currency": "EUR",
    }
    assert res1.json() == expected_cart_3

    # 2. Промяна на количеството на точно 5
    res2 = client.put(
        f"/cart/items/{product.id}", json={"quantity": 5}, headers=headers
    )
    assert res2.status_code == 200
    assert res2.json()["items"][0]["quantity"] == 5
    assert res2.json()["items"][0]["subtotal_minor"] == 7500
    assert res2.json()["total_minor"] == 7500

    # 3. Идемпотентност: повтаряне на същото количество връща идентичен резултат
    res3 = client.put(
        f"/cart/items/{product.id}", json={"quantity": 5}, headers=headers
    )
    assert res3.status_code == 200
    assert res3.json()["items"][0]["quantity"] == 5
    assert res3.json()["total_minor"] == 7500


# 4. Конкурентен тест за PUT върху един и същ продукт/потребител
def test_put_cart_item_concurrency(db_session: Session) -> None:
    """Критерий: Конкурентни PUT заявки за един и същ потребител и продукт не водят до колизии."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=20)
    headers = {"Authorization": f"Bearer {token}"}

    def _timed_test_db() -> Generator[Session]:
        with TestingSessionLocal() as session:
            session.execute(text("SET lock_timeout = '3000ms'"))
            session.execute(text("SET statement_timeout = '5000ms'"))
            yield session

    original_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = _timed_test_db

    num_workers = 8
    barrier = threading.Barrier(num_workers)

    def _send_put(qty: int) -> int:
        barrier.wait(timeout=5.0)
        r = client.put(
            f"/cart/items/{product.id}",
            json={"quantity": qty},
            headers=headers,
        )
        return r.status_code

    try:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(_send_put, i) for i in range(1, num_workers + 1)]
            status_codes = [f.result(timeout=10.0) for f in futures]
    finally:
        if original_override:
            app.dependency_overrides[get_db_session] = original_override
        else:
            app.dependency_overrides.pop(get_db_session, None)

    assert all(code == 200 for code in status_codes)

    # Проверка в базата: съществува точно един ред за (user_id, product_id)
    db_session.expire_all()
    items = (
        db_session.query(CartItem)
        .filter(CartItem.user_id == user.id, CartItem.product_id == product.id)
        .all()
    )
    assert len(items) == 1
    assert 1 <= items[0].quantity <= num_workers


# 5. Количка с няколко различни продукта и изчисления
def test_cart_with_multiple_products_calculations(db_session: Session) -> None:
    """Критерий: Коректно изчисляване на subtotal_minor, total_minor и възходящо подреждане по product_id."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    p1 = _create_product(db_session, title="Alpha Product", price_minor=1000, stock=10)
    p2 = _create_product(db_session, title="Beta Product", price_minor=500, stock=10)
    p3 = _create_product(db_session, title="Gamma Product", price_minor=250, stock=10)

    # Добавяме ги в разбъркан ред
    client.put(f"/cart/items/{p3.id}", json={"quantity": 2}, headers=headers)
    client.put(f"/cart/items/{p1.id}", json={"quantity": 3}, headers=headers)
    client.put(f"/cart/items/{p2.id}", json={"quantity": 4}, headers=headers)

    res = client.get("/cart", headers=headers)
    assert res.status_code == 200
    data = res.json()

    # Проверка на подредбата: възходящо по product_id
    item_ids = [item["product_id"] for item in data["items"]]
    assert item_ids == sorted([p1.id, p2.id, p3.id])

    # Проверка на сумите:
    # p1: 1000 * 3 = 3000
    # p2: 500 * 4 = 2000
    # p3: 250 * 2 = 500
    # total: 3000 + 2000 + 500 = 5500
    assert data["total_minor"] == 5500
    assert data["currency"] == "EUR"


# 6. Невалидно quantity връща 422
@pytest.mark.parametrize(
    "invalid_payload",
    [
        {"quantity": 0},
        {"quantity": 100},
        {"quantity": -1},
        {"quantity": 1.5},
        {"quantity": True},
        {"quantity": False},
        {"quantity": None},
        {"quantity": "5"},
        {},
        {"quantity": 5, "extra": "field"},
    ],
)
def test_put_invalid_quantity_returns_422(
    invalid_payload: dict, db_session: Session
) -> None:
    """Критерий: Невалидно quantity (0, 100, float, bool, null, extra) се отхвърля с 422."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    product = _create_product(db_session, stock=10)

    response = client.put(
        f"/cart/items/{product.id}",
        json=invalid_payload,
        headers=headers,
    )
    assert response.status_code == 422

    # Количката остава непроменена
    get_res = client.get("/cart", headers=headers)
    assert get_res.json()["items"] == []


# 7. Несъществуващ или деактивиран продукт при PUT връща 404 PRODUCT_NOT_FOUND
def test_put_missing_or_inactive_product_returns_404(db_session: Session) -> None:
    """Критерий: Липсващ или active=False продукт при PUT връща 404 PRODUCT_NOT_FOUND."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    # Несъществуващ продукт
    res_missing = client.put(
        "/cart/items/999999", json={"quantity": 1}, headers=headers
    )
    assert res_missing.status_code == 404
    assert res_missing.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

    # Деактивиран продукт
    inactive_product = _create_product(db_session, stock=10, active=False)
    res_inactive = client.put(
        f"/cart/items/{inactive_product.id}",
        json={"quantity": 1},
        headers=headers,
    )
    assert res_inactive.status_code == 404
    assert res_inactive.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"


# 8. Заявено quantity > stock при PUT връща 409 INSUFFICIENT_STOCK и не променя количката
def test_put_insufficient_stock_returns_409(db_session: Session) -> None:
    """Критерий: quantity > stock връща 409 INSUFFICIENT_STOCK и не прави промени."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    product = _create_product(db_session, stock=5)

    # 1. Опит за добавяне на 6 при наличност 5
    res1 = client.put(
        f"/cart/items/{product.id}", json={"quantity": 6}, headers=headers
    )
    assert res1.status_code == 409
    assert res1.json()["detail"]["code"] == "INSUFFICIENT_STOCK"

    # Проверка, че количката остава празна
    assert client.get("/cart", headers=headers).json()["items"] == []

    # 2. Добавяне на валидно количество 3
    client.put(f"/cart/items/{product.id}", json={"quantity": 3}, headers=headers)

    # 3. Опит за повишаване до 6 -> 409
    res2 = client.put(
        f"/cart/items/{product.id}", json={"quantity": 6}, headers=headers
    )
    assert res2.status_code == 409
    assert res2.json()["detail"]["code"] == "INSUFFICIENT_STOCK"

    # Количеството в количката остава непроменено (3)
    current_cart = client.get("/cart", headers=headers).json()
    assert current_cart["items"][0]["quantity"] == 3


# 9. Промяна на цената в каталога се отразява при следващ GET /cart
def test_price_change_reflected_in_get_cart(db_session: Session) -> None:
    """Критерий: Промяната на цената в каталога се отразява динамично при GET /cart."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    product = _create_product(db_session, price_minor=1000, stock=10)

    client.put(f"/cart/items/{product.id}", json={"quantity": 2}, headers=headers)

    # Променяме цената на продукта в каталога
    product.price_minor = 2500
    db_session.commit()

    res = client.get("/cart", headers=headers)
    assert res.status_code == 200
    item = res.json()["items"][0]
    assert item["unit_price_minor"] == 2500
    assert item["subtotal_minor"] == 5000
    assert res.json()["total_minor"] == 5000


# 10. Деактивиране или намаляване на stock прави available=false, но запазва сумата
def test_deactivation_or_stock_reduction_marks_unavailable(
    db_session: Session,
) -> None:
    """Критерий: Деактивиран или изчерпан продукт става available=False, но участва в total_minor."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    product = _create_product(db_session, price_minor=2000, stock=5, active=True)

    client.put(f"/cart/items/{product.id}", json={"quantity": 4}, headers=headers)

    # 1. Деактивиране на продукта в каталога
    product.active = False
    db_session.commit()

    res1 = client.get("/cart", headers=headers)
    assert res1.status_code == 200
    item1 = res1.json()["items"][0]
    assert item1["available"] is False
    assert item1["subtotal_minor"] == 8000
    assert res1.json()["total_minor"] == 8000

    # 2. Активиране, но намаляване на наличността под количеството в количката (stock=2 < quantity=4)
    product.active = True
    product.stock = 2
    db_session.commit()

    res2 = client.get("/cart", headers=headers)
    assert res2.status_code == 200
    item2 = res2.json()["items"][0]
    assert item2["available"] is False
    assert item2["subtotal_minor"] == 8000
    assert res2.json()["total_minor"] == 8000


# 11. Складовата наличност (stock) НЕ намалява след операции с количката
def test_stock_not_decreased_by_cart_operations(db_session: Session) -> None:
    """Критерий: Количката не резервира и не намалява складовата наличност."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    product = _create_product(db_session, stock=10)

    # 1. PUT операция
    client.put(f"/cart/items/{product.id}", json={"quantity": 4}, headers=headers)
    db_session.refresh(product)
    assert product.stock == 10

    # 2. GET операция
    client.get("/cart", headers=headers)
    db_session.refresh(product)
    assert product.stock == 10

    # 3. DELETE операция
    client.delete(f"/cart/items/{product.id}", headers=headers)
    db_session.refresh(product)
    assert product.stock == 10


# 12. DELETE премахва продукт, повторен DELETE връща 204, работи и за деактивиран продукт
def test_delete_cart_item(db_session: Session) -> None:
    """Критерий: DELETE премахва позиция, повторен DELETE е 204, работи и за деактивиран продукт."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    product = _create_product(db_session, stock=10)

    # Добавяме продукт
    client.put(f"/cart/items/{product.id}", json={"quantity": 2}, headers=headers)
    assert len(client.get("/cart", headers=headers).json()["items"]) == 1

    # 1. Успешно изтриване
    res_del = client.delete(f"/cart/items/{product.id}", headers=headers)
    assert res_del.status_code == 204
    assert res_del.content == b""
    assert client.get("/cart", headers=headers).json()["items"] == []

    # 2. Повторно изтриване на несъществуващ в количката продукт връща отново 204
    res_del_repeat = client.delete(f"/cart/items/{product.id}", headers=headers)
    assert res_del_repeat.status_code == 204

    # 3. Изтриване за продукт, който е деактивиран в каталога
    client.put(f"/cart/items/{product.id}", json={"quantity": 1}, headers=headers)
    product.active = False
    db_session.commit()

    res_del_inactive = client.delete(f"/cart/items/{product.id}", headers=headers)
    assert res_del_inactive.status_code == 204
    assert client.get("/cart", headers=headers).json()["items"] == []

    # 4. Изтриване на изцяло непознат product_id
    res_del_unknown = client.delete("/cart/items/999999", headers=headers)
    assert res_del_unknown.status_code == 204


# 13. Изолация между потребители
def test_cart_user_isolation(db_session: Session) -> None:
    """Критерий: Потребител A и потребител B имат напълно изолирани колички."""
    _, token_a = _create_user_and_token(db_session, email="user_a@example.com")
    _, token_b = _create_user_and_token(db_session, email="user_b@example.com")
    _, token_admin = _create_user_and_token(
        db_session, email="admin_cart@example.com", role="admin"
    )

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    headers_admin = {"Authorization": f"Bearer {token_admin}"}

    p1 = _create_product(db_session, title="P1", price_minor=1000, stock=10)
    p2 = _create_product(db_session, title="P2", price_minor=2000, stock=10)

    # User A слага P1 (qty 2)
    client.put(f"/cart/items/{p1.id}", json={"quantity": 2}, headers=headers_a)

    # User B слага P1 (qty 5) и P2 (qty 1)
    client.put(f"/cart/items/{p1.id}", json={"quantity": 5}, headers=headers_b)
    client.put(f"/cart/items/{p2.id}", json={"quantity": 1}, headers=headers_b)

    # Admin слага P2 (qty 3)
    client.put(f"/cart/items/{p2.id}", json={"quantity": 3}, headers=headers_admin)

    # Проверка на количката на A
    cart_a = client.get("/cart", headers=headers_a).json()
    assert len(cart_a["items"]) == 1
    assert cart_a["items"][0]["product_id"] == p1.id
    assert cart_a["items"][0]["quantity"] == 2
    assert cart_a["total_minor"] == 2000

    # Проверка на количката на B
    cart_b = client.get("/cart", headers=headers_b).json()
    assert len(cart_b["items"]) == 2
    assert cart_b["items"][0]["product_id"] == p1.id
    assert cart_b["items"][0]["quantity"] == 5
    assert cart_b["items"][1]["product_id"] == p2.id
    assert cart_b["items"][1]["quantity"] == 1
    assert cart_b["total_minor"] == 7000

    # Проверка на количката на Admin
    cart_admin = client.get("/cart", headers=headers_admin).json()
    assert len(cart_admin["items"]) == 1
    assert cart_admin["items"][0]["quantity"] == 3

    # User A изтрива P1 от своята количка
    client.delete(f"/cart/items/{p1.id}", headers=headers_a)
    assert client.get("/cart", headers=headers_a).json()["items"] == []

    # Количката на B остава незасегната
    cart_b_after = client.get("/cart", headers=headers_b).json()
    assert len(cart_b_after["items"]) == 2
    assert cart_b_after["items"][0]["quantity"] == 5


# 14. Ограничения на ниво база данни (Constraints & Foreign Keys)
def test_cart_database_constraints(db_session: Session) -> None:
    """Критерий: FK CASCADE, FK RESTRICT, съставен PK и CHECK constraint на ниво база."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10)

    # 1. Валиден запис
    item = CartItem(user_id=user.id, product_id=product.id, quantity=5)
    db_session.add(item)
    db_session.commit()

    # 2. CHECK constraint: quantity < 1 (директно през SQL)
    product_2 = _create_product(db_session, title="P2", stock=10)
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                f"INSERT INTO cart_items (user_id, product_id, quantity) "
                f"VALUES ({user.id}, {product_2.id}, 0)"
            )
        )
        db_session.commit()
    db_session.rollback()

    # 3. CHECK constraint: quantity > 99
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                f"INSERT INTO cart_items (user_id, product_id, quantity) "
                f"VALUES ({user.id}, {product_2.id}, 100)"
            )
        )
        db_session.commit()
    db_session.rollback()

    # 4. Съставен първичен ключ (дублиран запис за същия потребител и продукт)
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                f"INSERT INTO cart_items (user_id, product_id, quantity) "
                f"VALUES ({user.id}, {product.id}, 2)"
            )
        )
        db_session.commit()
    db_session.rollback()

    # 5. FK RESTRICT: опит за изтриване на продукт, който е в количка, бива отхвърлен
    with pytest.raises(IntegrityError):
        db_session.execute(text(f"DELETE FROM products WHERE id = {product.id}"))
        db_session.commit()
    db_session.rollback()

    # 6. FK CASCADE: изтриване на потребител изтрива каскадно неговите cart_items
    db_session.execute(text(f"DELETE FROM users WHERE id = {user.id}"))
    db_session.commit()
    remaining_items = (
        db_session.query(CartItem).filter(CartItem.user_id == user.id).all()
    )
    assert remaining_items == []
