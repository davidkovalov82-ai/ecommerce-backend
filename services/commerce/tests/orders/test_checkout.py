import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Product
from app.modules.orders.models import Order
from tests.orders.helpers import (
    _add_to_cart,
    _create_product,
    _create_user_and_token,
)

client = TestClient(app)


def test_create_order_validation(db_session: Session) -> None:
    """Критерий: Липсващ/невалиден Idempotency-Key или невалидно тяло връщат 422."""
    _, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Липсващ Idempotency-Key
    r1 = client.post("/orders", json={}, headers=headers)
    assert r1.status_code == 422

    # 2. Невалиден UUID в Idempotency-Key
    r2 = client.post(
        "/orders",
        json={},
        headers={**headers, "Idempotency-Key": "not-a-valid-uuid"},
    )
    assert r2.status_code == 422

    # 3. Невалидно тяло (съдържа допълнителни полета)
    r3 = client.post(
        "/orders",
        json={"notes": "please rush"},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r3.status_code == 422

    # 4. Невалидно тяло (не е JSON обект)
    r4 = client.post(
        "/orders",
        content="[1, 2, 3]",
        headers={
            **headers,
            "Idempotency-Key": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
    )
    assert r4.status_code == 422


def test_create_order_empty_cart(db_session: Session) -> None:
    """Критерий: Оформяне на празна количка връща 409 CART_EMPTY."""
    _, token = _create_user_and_token(db_session)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
    }

    r = client.post("/orders", json={}, headers=headers)
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["code"] == "CART_EMPTY"


def test_create_order_inactive_product(db_session: Session) -> None:
    """Критерий: Неактивен продукт в количката връща 409 CART_PRODUCT_UNAVAILABLE с product_id."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10, active=False)
    _add_to_cart(db_session, user.id, product.id, quantity=2)

    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
    }
    r = client.post("/orders", json={}, headers=headers)
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["code"] == "CART_PRODUCT_UNAVAILABLE"
    assert body["detail"]["product_id"] == product.id

    # Проверка, че наличността и количката са непокътнати и няма поръчка
    db_session.expire_all()
    assert db_session.query(CartItem).filter(CartItem.user_id == user.id).count() == 1
    assert db_session.query(Order).filter(Order.user_id == user.id).count() == 0


def test_create_order_insufficient_stock(db_session: Session) -> None:
    """Критерий: Недостатъчна наличност връща 409 INSUFFICIENT_STOCK с product_id."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=3, active=True)
    _add_to_cart(db_session, user.id, product.id, quantity=5)

    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
    }
    r = client.post("/orders", json={}, headers=headers)
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["code"] == "INSUFFICIENT_STOCK"
    assert body["detail"]["product_id"] == product.id

    # Проверка за непроменена база данни
    db_session.expire_all()
    refreshed_prod = db_session.get(Product, product.id)
    assert refreshed_prod is not None
    assert refreshed_prod.stock == 3
    assert db_session.query(Order).filter(Order.user_id == user.id).count() == 0


def test_create_order_success(db_session: Session) -> None:
    """Критерий: Успешно оформяне на няколко продукта, точни суми, намален склад и изчистена количка."""
    user, token = _create_user_and_token(db_session)
    prod1 = _create_product(db_session, title="Notebook", price_minor=10000, stock=10)
    prod2 = _create_product(db_session, title="Mouse", price_minor=2500, stock=5)

    _add_to_cart(db_session, user.id, prod1.id, quantity=2)
    _add_to_cart(db_session, user.id, prod2.id, quantity=3)

    key = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }

    r = client.post("/orders", json={}, headers=headers)
    assert r.status_code == 201
    data = r.json()

    assert data["status"] == "pending_payment"
    assert data["currency"] == "EUR"
    expected_total = 2 * 10000 + 3 * 2500  # 20000 + 7500 = 27500
    assert data["total_minor"] == expected_total
    assert "created_at" in data

    items = data["items"]
    assert len(items) == 2
    # Стабилно подреждане по product_id възходящо
    assert items[0]["product_id"] == prod1.id
    assert items[0]["product_title"] == "Notebook"
    assert items[0]["quantity"] == 2
    assert items[0]["unit_price_minor"] == 10000
    assert items[0]["subtotal_minor"] == 20000

    assert items[1]["product_id"] == prod2.id
    assert items[1]["product_title"] == "Mouse"
    assert items[1]["quantity"] == 3
    assert items[1]["unit_price_minor"] == 2500
    assert items[1]["subtotal_minor"] == 7500

    # Проверка в БД: складът е намален коректно
    db_session.expire_all()
    p1 = db_session.get(Product, prod1.id)
    p2 = db_session.get(Product, prod2.id)
    assert p1 is not None and p1.stock == 8
    assert p2 is not None and p2.stock == 2

    # Количката е напълно изчистена
    cart_count = db_session.query(CartItem).filter(CartItem.user_id == user.id).count()
    assert cart_count == 0


def test_order_snapshot_preserved_after_catalog_changes(
    db_session: Session,
) -> None:
    """Критерий: Поръчката запазва заглавията и цените към момента на създаването."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(
        db_session, title="Original Keyboard", price_minor=5000, stock=5
    )
    _add_to_cart(db_session, user.id, product.id, quantity=1)

    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
    r = client.post("/orders", json={}, headers=headers)
    assert r.status_code == 201
    order_id = r.json()["id"]

    # Променяме заглавието и цената в каталога
    product.title = "Modified Keyboard V2"
    product.price_minor = 9999
    db_session.commit()

    # Извличаме поръчката отново
    r_get = client.get(
        f"/orders/{order_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert r_get.status_code == 200
    detail = r_get.json()
    assert detail["items"][0]["product_title"] == "Original Keyboard"
    assert detail["items"][0]["unit_price_minor"] == 5000
    assert detail["items"][0]["subtotal_minor"] == 5000
    assert detail["total_minor"] == 5000
