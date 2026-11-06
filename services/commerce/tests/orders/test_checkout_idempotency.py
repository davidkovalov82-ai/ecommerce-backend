import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Product
from tests.orders.helpers import (
    _add_to_cart,
    _create_product,
    _create_user_and_token,
)

client = TestClient(app)


def test_create_order_idempotency_same_user(db_session: Session) -> None:
    """Критерий: Повторен POST от същия потребител със същия ключ връща същата поръчка с 201, без нов stock decrement."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10, price_minor=2000)
    _add_to_cart(db_session, user.id, product.id, quantity=2)

    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    # 1. Първо създаване
    r1 = client.post("/orders", json={}, headers=headers)
    assert r1.status_code == 201
    order1_id = r1.json()["id"]

    # Потребителят добавя нов продукт в количката след първата поръчка
    prod_new = _create_product(db_session, stock=5, price_minor=1000)
    _add_to_cart(db_session, user.id, prod_new.id, quantity=1)

    # 2. Повторно извикване със същия ключ
    r2 = client.post("/orders", json={}, headers=headers)
    assert r2.status_code == 201
    assert r2.json()["id"] == order1_id

    # Проверка, че складът не е намален втори път
    db_session.expire_all()
    refreshed_p = db_session.get(Product, product.id)
    assert refreshed_p is not None and refreshed_p.stock == 8

    # Новата позиция в количката не е изтрита
    cart_items = db_session.query(CartItem).filter(CartItem.user_id == user.id).all()
    assert len(cart_items) == 1
    assert cart_items[0].product_id == prod_new.id


def test_create_order_idempotency_retry_after_failed_transaction(
    db_session: Session,
) -> None:
    """Критерий: Неуспешна транзакция не запазва ключа и позволява повторен успешен опит със същия ключ."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=1, price_minor=1000)
    _add_to_cart(db_session, user.id, product.id, quantity=3)

    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    # 1. Първи опит: недостатъчен склад -> 409
    r1 = client.post("/orders", json={}, headers=headers)
    assert r1.status_code == 409

    # Администраторът увеличава наличността
    product.stock = 10
    db_session.commit()

    # 2. Повторен опит със СЪЩИЯ ключ -> сега трябва да успее с 201
    r2 = client.post("/orders", json={}, headers=headers)
    assert r2.status_code == 201
    assert r2.json()["status"] == "pending_payment"

    db_session.expire_all()
    refreshed_p = db_session.get(Product, product.id)
    assert refreshed_p is not None and refreshed_p.stock == 7


def test_same_idempotency_key_different_users(db_session: Session) -> None:
    """Критерий: Един и същ UUID може да се използва от различни потребители без конфликт."""
    user_a, token_a = _create_user_and_token(db_session, email="a@example.com")
    user_b, token_b = _create_user_and_token(db_session, email="b@example.com")

    prod = _create_product(db_session, stock=20, price_minor=1000)
    _add_to_cart(db_session, user_a.id, prod.id, quantity=1)
    _add_to_cart(db_session, user_b.id, prod.id, quantity=2)

    shared_key = str(uuid.uuid4())

    r_a = client.post(
        "/orders",
        json={},
        headers={
            "Authorization": f"Bearer {token_a}",
            "Idempotency-Key": shared_key,
        },
    )
    assert r_a.status_code == 201

    r_b = client.post(
        "/orders",
        json={},
        headers={
            "Authorization": f"Bearer {token_b}",
            "Idempotency-Key": shared_key,
        },
    )
    assert r_b.status_code == 201

    assert r_a.json()["id"] != r_b.json()["id"]


def test_order_total_minor_exceeds_32bit_integer(db_session: Session) -> None:
    """Критерий: Сумите надхвърлят 32-битов Integer без препълване благодарение на BigInteger."""
    user, token = _create_user_and_token(db_session)
    # 20 000 000 EUR (2 милиарда цента) * 20 бр = 40 000 000 000 цента (> 2 147 483 647)
    big_price = 2_000_000_000
    prod = _create_product(
        db_session, title="Super Yacht", price_minor=big_price, stock=20
    )
    _add_to_cart(db_session, user.id, prod.id, quantity=20)

    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
    }
    r = client.post("/orders", json={}, headers=headers)
    assert r.status_code == 201
    data = r.json()

    expected_total = big_price * 20
    assert data["total_minor"] == expected_total
    assert data["items"][0]["subtotal_minor"] == expected_total

    # Проверка и през GET
    r_get = client.get(
        f"/orders/{data['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert r_get.status_code == 200
    assert r_get.json()["total_minor"] == expected_total
