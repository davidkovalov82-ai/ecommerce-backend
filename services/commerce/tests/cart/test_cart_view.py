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
    email: str = "customer_view@example.com",
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
