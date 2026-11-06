import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token
from app.modules.catalog.models import Product

client = TestClient(app)


def _create_user_and_token(
    db_session: Session,
    email: str = "customer_items@example.com",
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
