import threading
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import Request
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
from app.modules.orders.models import Order, OrderItem
from tests.conftest import TestingSessionLocal

client = TestClient(app)


def _create_user_and_token(
    db_session: Session,
    email: str = "customer@example.com",
    role: str = "customer",
) -> tuple[User, str]:
    """Създава тестов потребител и валиден Bearer токен."""
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
    """Създава тестов продукт в каталога."""
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


def _add_to_cart(
    db_session: Session,
    user_id: int,
    product_id: int,
    quantity: int = 1,
) -> None:
    """Директно добавя позиция в количката на потребителя."""
    cart_item = CartItem(
        user_id=user_id,
        product_id=product_id,
        quantity=quantity,
    )
    db_session.add(cart_item)
    db_session.commit()


# ============================================================================
# 1. Автентикация и валидация на входа
# ============================================================================


def test_orders_auth_required() -> None:
    """Критерий: Без валидна автентикация всички поръчкови endpoint-и връщат 401."""
    # POST /orders
    r_post = client.post(
        "/orders",
        json={},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r_post.status_code == 401

    # GET /orders
    r_get_list = client.get("/orders")
    assert r_get_list.status_code == 401

    # GET /orders/{order_id}
    r_get_detail = client.get("/orders/1")
    assert r_get_detail.status_code == 401


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


# ============================================================================
# 2. Бизнес грешки при оформяне (409)
# ============================================================================


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


# ============================================================================
# 3. Успешно оформяне и запазване на исторически стойности (201)
# ============================================================================


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


# ============================================================================
# 4. Идемпотентност
# ============================================================================


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


def test_late_rollback_on_simulated_error_before_commit(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Критерий: Симулирана грешка след създаване на поръчка, позиции, намаляване на stock и изчистване на количка (след flush, преди commit).

    След rollback през отделна сесия:
    - няма поръчка и позиции;
    - наличността е възстановена;
    - количката е непроменена.
    След премахване на грешката, повторен опит със същия Idempotency-Key успява точно веднъж.
    """
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10, price_minor=1000)
    _add_to_cart(db_session, user.id, product.id, quantity=2)

    idempotency_key = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": idempotency_key,
    }

    original_commit = Session.commit

    def _failing_commit(self: Session) -> None:
        raise RuntimeError("Simulated failure right before commit")

    monkeypatch.setattr(Session, "commit", _failing_commit)

    with pytest.raises(RuntimeError, match="Simulated failure right before commit"):
        client.post("/orders", json={}, headers=headers)

    # Проверка през НАПЪЛНО ОТДЕЛНА сесия след rollback
    with TestingSessionLocal() as verification_session:
        # 1. Няма поръчка и няма позиции от неуспешния опит
        assert (
            verification_session.query(Order).filter(Order.user_id == user.id).count()
            == 0
        )
        assert verification_session.query(OrderItem).count() == 0

        # 2. Наличността е възстановена на първоначалните 10
        refreshed_p = verification_session.get(Product, product.id)
        assert refreshed_p is not None
        assert refreshed_p.stock == 10

        # 3. Количката е непроменена (съдържа продукта с quantity=2)
        cart_items = (
            verification_session.query(CartItem)
            .filter(CartItem.user_id == user.id)
            .all()
        )
        assert len(cart_items) == 1
        assert cart_items[0].product_id == product.id
        assert cart_items[0].quantity == 2

    # Премахваме симулираната грешка
    monkeypatch.setattr(Session, "commit", original_commit)

    # Повтаряме със СЪЩИЯ Idempotency-Key
    r_retry = client.post("/orders", json={}, headers=headers)
    assert r_retry.status_code == 201
    order_data = r_retry.json()
    assert order_data["status"] == "pending_payment"
    assert order_data["total_minor"] == 2000
    assert len(order_data["items"]) == 1

    # Проверка през отделна сесия, че поръчката е създадена точно веднъж
    with TestingSessionLocal() as verification_session:
        assert (
            verification_session.query(Order).filter(Order.user_id == user.id).count()
            == 1
        )
        refreshed_p2 = verification_session.get(Product, product.id)
        assert refreshed_p2 is not None
        assert refreshed_p2.stock == 8
        assert (
            verification_session.query(CartItem)
            .filter(CartItem.user_id == user.id)
            .count()
            == 0
        )

    # Трето повторение със същия ключ -> връща същата поръчка (идемпотентност)
    r_idempotent = client.post("/orders", json={}, headers=headers)
    assert r_idempotent.status_code == 201
    assert r_idempotent.json()["id"] == order_data["id"]


# ============================================================================
# 5. Справки и пагинация
# ============================================================================


def test_get_orders_pagination_and_sorting(db_session: Session) -> None:
    """Критерий: GET /orders връща само собствени поръчки, пагинирани и сортирани по created_at DESC, id DESC."""
    user, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    prod = _create_product(db_session, stock=100, price_minor=1000)

    order_ids: list[int] = []
    for _ in range(5):
        _add_to_cart(db_session, user.id, prod.id, quantity=1)
        r = client.post(
            "/orders",
            json={},
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201
        order_ids.append(r.json()["id"])

    # Първа страница: limit 2, offset 0
    r_page1 = client.get("/orders?limit=2&offset=0", headers=headers)
    assert r_page1.status_code == 200
    p1 = r_page1.json()
    assert p1["total"] == 5
    assert p1["limit"] == 2
    assert p1["offset"] == 0
    assert len(p1["items"]) == 2
    # Проверка, че няма вложени items в списъка
    assert "items" not in p1["items"][0]
    # Най-новите са първи
    assert p1["items"][0]["id"] == order_ids[4]
    assert p1["items"][1]["id"] == order_ids[3]

    # Втора страница: limit 2, offset 2
    r_page2 = client.get("/orders?limit=2&offset=2", headers=headers)
    assert r_page2.status_code == 200
    p2 = r_page2.json()
    assert len(p2["items"]) == 2
    assert p2["items"][0]["id"] == order_ids[2]
    assert p2["items"][1]["id"] == order_ids[1]

    # Трета страница: limit 2, offset 4
    r_page3 = client.get("/orders?limit=2&offset=4", headers=headers)
    assert r_page3.status_code == 200
    p3 = r_page3.json()
    assert len(p3["items"]) == 1
    assert p3["items"][0]["id"] == order_ids[0]


def test_get_order_by_id_isolation(db_session: Session) -> None:
    """Критерий: Потребител не може да достъпи чужда поръчка; получава 404 ORDER_NOT_FOUND."""
    user_a, token_a = _create_user_and_token(db_session, email="a@example.com")
    _user_b, token_b = _create_user_and_token(db_session, email="b@example.com")
    prod = _create_product(db_session, stock=10)

    _add_to_cart(db_session, user_a.id, prod.id, quantity=1)
    r_create = client.post(
        "/orders",
        json={},
        headers={
            "Authorization": f"Bearer {token_a}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert r_create.status_code == 201
    order_a_id = r_create.json()["id"]

    # Потребител B опитва да прочете поръчката на A
    r_b = client.get(
        f"/orders/{order_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert r_b.status_code == 404
    assert r_b.json()["detail"]["code"] == "ORDER_NOT_FOUND"

    # Несъществуваща поръчка
    r_none = client.get(
        "/orders/999999",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r_none.status_code == 404
    assert r_none.json()["detail"]["code"] == "ORDER_NOT_FOUND"


# ============================================================================
# 6. Суми надхвърлящи 32-битов Integer
# ============================================================================


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


# ============================================================================
# 7. Конкурентност и състезания (Race Conditions)
# ============================================================================


def test_concurrent_same_key_creates_single_order(db_session: Session) -> None:
    """Критерий: Конкурентни заявки с един и същ ключ създават точно една поръчка и не дублират намаляването на склада."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=20, price_minor=1000)
    _add_to_cart(db_session, user.id, product.id, quantity=2)

    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    def _timed_db() -> Generator[Session]:
        with TestingSessionLocal() as session:
            session.execute(text("SET lock_timeout = '3000ms'"))
            session.execute(text("SET statement_timeout = '5000ms'"))
            yield session

    original_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = _timed_db

    num_workers = 6
    barrier = threading.Barrier(num_workers)

    def _send_post() -> tuple[int, int]:
        barrier.wait(timeout=5.0)
        r = client.post("/orders", json={}, headers=headers)
        return r.status_code, r.json().get("id", 0)

    try:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(_send_post) for _ in range(num_workers)]
            results = [f.result(timeout=10.0) for f in futures]
    finally:
        if original_override:
            app.dependency_overrides[get_db_session] = original_override
        else:
            app.dependency_overrides.pop(get_db_session, None)

    # Всички заявки трябва да върнат 201 с абсолютно същото order_id
    assert all(status == 201 for status, _ in results)
    order_ids = [order_id for _, order_id in results]
    assert len(set(order_ids)) == 1

    # В базата има точно 1 поръчка и складът е намален точно с 2 (10 - 2 = 18)
    db_session.expire_all()
    assert db_session.query(Order).filter(Order.user_id == user.id).count() == 1
    p = db_session.get(Product, product.id)
    assert p is not None and p.stock == 18


def test_two_users_compete_for_last_stock(db_session: Session) -> None:
    """Критерий: Двама потребители купуват последната бройка: една успешна поръчка, един 409 отказ, stock = 0."""
    user_a, token_a = _create_user_and_token(db_session, email="a@example.com")
    user_b, token_b = _create_user_and_token(db_session, email="b@example.com")
    product = _create_product(db_session, stock=1, price_minor=1000)

    _add_to_cart(db_session, user_a.id, product.id, quantity=1)
    _add_to_cart(db_session, user_b.id, product.id, quantity=1)

    def _timed_db() -> Generator[Session]:
        with TestingSessionLocal() as session:
            session.execute(text("SET lock_timeout = '3000ms'"))
            session.execute(text("SET statement_timeout = '5000ms'"))
            yield session

    original_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = _timed_db

    barrier = threading.Barrier(2)

    def _attempt_order(tok: str) -> int:
        barrier.wait(timeout=5.0)
        r = client.post(
            "/orders",
            json={},
            headers={
                "Authorization": f"Bearer {tok}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        return r.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            fa = executor.submit(_attempt_order, token_a)
            fb = executor.submit(_attempt_order, token_b)
            statuses = [fa.result(timeout=10.0), fb.result(timeout=10.0)]
    finally:
        if original_override:
            app.dependency_overrides[get_db_session] = original_override
        else:
            app.dependency_overrides.pop(get_db_session, None)

    # Точно една заявка успява (201), а другата бива отхвърлена (409)
    assert 201 in statuses
    assert 409 in statuses

    db_session.expire_all()
    p = db_session.get(Product, product.id)
    assert p is not None and p.stock == 0
    assert db_session.query(Order).count() == 1


def test_concurrent_checkouts_same_cart_different_keys(
    db_session: Session,
) -> None:
    """Критерий: Две едновременни оформления на една количка с различни ключове:

    една поръчка, а вторият опит вижда празна количка (409 CART_EMPTY).
    """
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10, price_minor=1000)
    _add_to_cart(db_session, user.id, product.id, quantity=1)

    def _timed_db() -> Generator[Session]:
        with TestingSessionLocal() as session:
            session.execute(text("SET lock_timeout = '3000ms'"))
            session.execute(text("SET statement_timeout = '5000ms'"))
            yield session

    original_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = _timed_db

    barrier = threading.Barrier(2)

    def _attempt_order() -> int:
        barrier.wait(timeout=5.0)
        r = client.post(
            "/orders",
            json={},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        return r.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_attempt_order)
            f2 = executor.submit(_attempt_order)
            statuses = [f1.result(timeout=10.0), f2.result(timeout=10.0)]
    finally:
        if original_override:
            app.dependency_overrides[get_db_session] = original_override
        else:
            app.dependency_overrides.pop(get_db_session, None)

    # Едната успява (201), а втората вижда празна количка (409)
    assert 201 in statuses
    assert 409 in statuses

    db_session.expire_all()
    assert db_session.query(Order).filter(Order.user_id == user.id).count() == 1


@pytest.mark.parametrize("scenario", ["put_first", "checkout_first"])
def test_parallel_checkout_and_cart_modification(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Критерий: Паралелен checkout и промяна на количката се сериализират от общата DB блокировка на потребителя.

    Доказва, че никога не се губят позиции при нито един от двата реда на изпълнение:
    А) PUT се изпълнява преди checkout:
       - p2 присъства в поръчката с quantity=3;
       - p2 липсва от количката;
       - stock на p2 е 7.
    Б) Checkout се изпълнява преди PUT:
       - p2 липсва от поръчката;
       - p2 остава в количката с quantity=3;
       - stock на p2 е 10.
    И в двата случая:
       - p1 присъства в поръчката с quantity=1 и stock на p1 е 9;
       - p2 не липсва и от поръчката, и от количката;
       - няма дублиране на позиции.
    """
    user, token = _create_user_and_token(db_session)
    p1 = _create_product(db_session, stock=10, price_minor=1000)
    p2 = _create_product(db_session, stock=10, price_minor=2000)
    _add_to_cart(db_session, user.id, p1.id, quantity=1)

    def _sync_db(request: Request) -> Generator[Session]:
        with TestingSessionLocal() as session:
            session.execute(text("SET lock_timeout = '3000ms'"))
            session.execute(text("SET statement_timeout = '5000ms'"))
            if request.method == "POST" and request.url.path.rstrip("/") == "/orders":
                session.info["operation"] = "checkout"
            elif request.method == "PUT" and request.url.path.startswith(
                "/cart/items/"
            ):
                session.info["operation"] = "cart_put"
            yield session

    original_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = _sync_db

    first_op = "cart_put" if scenario == "put_first" else "checkout"
    second_op = "checkout" if scenario == "put_first" else "cart_put"

    op1_locked = threading.Event()
    op2_about_to_lock = threading.Event()
    op1_proceed = threading.Event()

    from app.modules.accounts.repository import UserRepository

    original_lock_by_id = UserRepository.lock_by_id

    def _sync_lock_by_id(self: UserRepository, user_id: int):
        op = self._session.info.get("operation")
        if op == first_op:
            # 1. Първата операция действително придобива DB блокировката:
            user_row = original_lock_by_id(self, user_id)
            op1_locked.set()

            # 3. Първата операция получава разрешение да продължи:
            if not op1_proceed.wait(timeout=5.0):
                raise TimeoutError(
                    f"Operation 1 ({first_op}) timed out waiting for permission to proceed"
                )
            return user_row

        if op == second_op:
            # Изчакваме първата операция действително да придобие DB блокировката:
            if not op1_locked.wait(timeout=5.0):
                raise TimeoutError(
                    f"Operation 2 ({second_op}) timed out waiting for operation 1 ({first_op}) to acquire DB lock"
                )

            # 2. Втората операция достига опита за придобиване на същата блокировка:
            op2_about_to_lock.set()
            # Разрешаваме на първата операция да продължи:
            op1_proceed.set()

            # Втората операция се опитва да придобие същата блокировка (блокира се в PostgreSQL):
            return original_lock_by_id(self, user_id)

        return original_lock_by_id(self, user_id)

    monkeypatch.setattr(UserRepository, "lock_by_id", _sync_lock_by_id)

    idempotency_key = str(uuid.uuid4())

    def _do_checkout() -> int:
        r = client.post(
            "/orders",
            json={},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": idempotency_key,
            },
        )
        return r.status_code

    def _do_cart_put() -> int:
        r = client.put(
            f"/cart/items/{p2.id}",
            json={"quantity": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
        return r.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            f_checkout = executor.submit(_do_checkout)
            f_cart = executor.submit(_do_cart_put)
            checkout_status = f_checkout.result(timeout=10.0)
            cart_status = f_cart.result(timeout=10.0)
    finally:
        # Освобождаваме чакащи операции във finally при всякакви обстоятелства
        op1_locked.set()
        op2_about_to_lock.set()
        op1_proceed.set()
        monkeypatch.undo()
        if original_override:
            app.dependency_overrides[get_db_session] = original_override
        else:
            app.dependency_overrides.pop(get_db_session, None)

    assert op1_locked.is_set(), f"Operation 1 ({first_op}) failed to acquire lock"
    assert op2_about_to_lock.is_set(), (
        f"Operation 2 ({second_op}) failed to reach lock attempt"
    )
    assert op1_proceed.is_set(), (
        f"Operation 1 ({first_op}) never received permission to proceed"
    )
    assert checkout_status == 201
    assert cart_status == 200

    # Проверка през отделна чиста сесия
    with TestingSessionLocal() as session:
        order = session.query(Order).filter(Order.user_id == user.id).one()
        order_items_by_pid = {item.product_id: item for item in order.items}
        cart_items_by_pid = {
            item.product_id: item
            for item in session.query(CartItem)
            .filter(CartItem.user_id == user.id)
            .all()
        }
        refreshed_p1 = session.get(Product, p1.id)
        refreshed_p2 = session.get(Product, p2.id)

        assert refreshed_p1 is not None
        assert refreshed_p2 is not None

        # Проверка на p1 (винаги е в поръчката с quantity=1, а наличността му става 9)
        assert p1.id in order_items_by_pid
        assert order_items_by_pid[p1.id].quantity == 1
        assert refreshed_p1.stock == 9
        assert p1.id not in cart_items_by_pid

        p2_in_order = p2.id in order_items_by_pid
        p2_in_cart = p2.id in cart_items_by_pid

        # Доказваме, че p2 не липсва и от поръчката, и от количката!
        assert p2_in_order or p2_in_cart
        # И че не се дублира едновременно в двете
        assert not (p2_in_order and p2_in_cart)

        if scenario == "put_first":
            # А) PUT се изпълнява преди checkout:
            # - p2 присъства в поръчката с quantity=3;
            # - p2 липсва от количката;
            # - stock на p2 е 7.
            assert p2_in_order
            assert not p2_in_cart
            assert order_items_by_pid[p2.id].quantity == 3
            assert refreshed_p2.stock == 7
            assert order.total_minor == 1 * 1000 + 3 * 2000  # 7000
        else:
            # Б) Checkout се изпълнява преди PUT:
            # - p2 липсва от поръчката;
            # - p2 остава в количката с quantity=3;
            # - stock на p2 е 10.
            assert not p2_in_order
            assert p2_in_cart
            assert cart_items_by_pid[p2.id].quantity == 3
            assert refreshed_p2.stock == 10
            assert order.total_minor == 1 * 1000  # 1000


# ============================================================================
# 8. Директни проверки на ограниченията в БД (DB Constraints)
# ============================================================================


def test_orders_database_constraints(db_session: Session) -> None:
    """Критерий: UNIQUE, CHECK и FOREIGN KEY ограниченията в orders и order_items работят на ниво PostgreSQL."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session)

    # 1. Валидна поръчка
    valid_order = Order(
        user_id=user.id,
        status="pending_payment",
        currency="EUR",
        total_minor=1000,
        idempotency_key="key-1",
    )
    db_session.add(valid_order)
    db_session.commit()

    # 2. UNIQUE(user_id, idempotency_key) дубликат
    duplicate_key_order = Order(
        user_id=user.id,
        status="pending_payment",
        currency="EUR",
        total_minor=2000,
        idempotency_key="key-1",
    )
    db_session.add(duplicate_key_order)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 3. CHECK status = 'pending_payment'
    invalid_status_order = Order(
        user_id=user.id,
        status="shipped",
        currency="EUR",
        total_minor=1000,
        idempotency_key="key-2",
    )
    db_session.add(invalid_status_order)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 4. CHECK currency = 'EUR'
    invalid_currency_order = Order(
        user_id=user.id,
        status="pending_payment",
        currency="USD",
        total_minor=1000,
        idempotency_key="key-3",
    )
    db_session.add(invalid_currency_order)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 5. RESTRICT на изтриване на потребител с поръчки
    with pytest.raises(IntegrityError):
        db_session.execute(text(f"DELETE FROM users WHERE id = {user.id}"))
        db_session.commit()
    db_session.rollback()

    # 6. RESTRICT на изтриване на продукт с поръчки
    order_item = OrderItem(
        order_id=valid_order.id,
        product_id=product.id,
        product_title=product.title,
        quantity=1,
        unit_price_minor=1000,
        subtotal_minor=1000,
    )
    db_session.add(order_item)
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(text(f"DELETE FROM products WHERE id = {product.id}"))
        db_session.commit()
    db_session.rollback()
