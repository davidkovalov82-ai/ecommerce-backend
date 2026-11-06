import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem, PaymentAttempt
from tests.conftest import TestingSessionLocal

client = TestClient(app)


def _create_user_and_token(
    db_session: Session,
    email: str = "payment_user@example.com",
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
    title: str = "Payment Test Product",
    price_minor: int = 2500,
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


def _create_order(
    db_session: Session,
    user_id: int,
    product: Product,
    quantity: int = 2,
    status: str = "pending_payment",
) -> Order:
    """Директно създава поръчка със запазена позиция и резервиран stock."""
    total_minor = product.price_minor * quantity
    order = Order(
        user_id=user_id,
        status=status,
        currency="EUR",
        total_minor=total_minor,
        idempotency_key=str(uuid.uuid4()),
    )
    db_session.add(order)
    db_session.flush()

    order_item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        product_title=product.title,
        quantity=quantity,
        unit_price_minor=product.price_minor,
        subtotal_minor=total_minor,
    )
    db_session.add(order_item)
    product.stock -= quantity
    db_session.commit()
    db_session.refresh(order)
    return order


# ============================================================================
# 1. Симулаторен endpoint: премахнат и не може да променя статус
# ============================================================================


def test_simulator_endpoint_removed_cannot_change_status(
    db_session: Session,
) -> None:
    """Критерий: Публичният POST /orders/{id}/payments е премахнат и не може да обяви поръчка за платена."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock = 8
    initial_stock = product.stock

    # Опит за извикване на стария симулаторен endpoint с test_success
    resp = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    # Тъй като endpoint-ът е премахнат, FastAPI връща 404 Not Found (или 405)
    assert resp.status_code in (404, 405)

    # Проверка в базата: поръчката ЗАДЪЛЖИТЕЛНО остава в pending_payment
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == "pending_payment"
    assert product.stock == initial_stock


# ============================================================================
# 2. Отказ от поръчка (Cancellation) и възстановяване на наличности
# ============================================================================


def test_cancel_auth_required() -> None:
    """Критерий: Без автентикация отказът връща 401."""
    resp = client.post("/orders/1/cancel")
    assert resp.status_code == 401


def test_cancel_foreign_or_missing_order_returns_404(
    db_session: Session,
) -> None:
    """Критерий: Чужда или несъществуваща поръчка връща 404 ORDER_NOT_FOUND."""
    user1, token1 = _create_user_and_token(db_session, email="u1@example.com")
    _user2, token2 = _create_user_and_token(db_session, email="u2@example.com")
    product = _create_product(db_session)
    order1 = _create_order(db_session, user1.id, product)

    # Несъществуваща поръчка
    r_non_existent = client.post(
        "/orders/999999/cancel",
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert r_non_existent.status_code == 404
    assert r_non_existent.json()["detail"]["code"] == "ORDER_NOT_FOUND"

    # Чужда поръчка
    r_foreign = client.post(
        f"/orders/{order1.id}/cancel",
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert r_foreign.status_code == 404
    assert r_foreign.json()["detail"]["code"] == "ORDER_NOT_FOUND"


def test_cancel_order_restores_stock_and_double_cancel(db_session: Session) -> None:
    """Критерий: Отказ от поръчка възстановява точно запазените бройки. Двоен отказ връща 200 и не дублира stock."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10)
    order = _create_order(db_session, user.id, product, quantity=4)  # stock стана 6
    assert product.stock == 6

    # 1. Първи отказ -> 200 OK
    r1 = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200
    data1 = r1.json()
    assert data1["id"] == order.id
    assert data1["status"] == "cancelled"

    # Наличността е възстановена с +4 (става отново 10)
    with TestingSessionLocal() as session:
        db_product = session.get(Product, product.id)
        assert db_product is not None
        assert db_product.stock == 10

        db_order = session.get(Order, order.id)
        assert db_order is not None
        assert db_order.status == "cancelled"

    # 2. Втори отказ (двоен отказ) -> 200 OK (идемпотентно), без повторно връщане на stock
    r2 = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "cancelled"

    # Наличността остава точно 10 (НЕ става 14!)
    with TestingSessionLocal() as session:
        db_product = session.get(Product, product.id)
        assert db_product is not None
        assert db_product.stock == 10


def test_cancel_order_restores_stock_for_deactivated_product(
    db_session: Session,
) -> None:
    """Критерий: При отказ stock се възстановява дори за продукт, деактивиран след checkout."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10, active=True)
    order = _create_order(db_session, user.id, product, quantity=3)  # stock=7

    # Деактивираме продукта в каталога
    with TestingSessionLocal() as session:
        p = session.get(Product, product.id)
        assert p is not None
        p.active = False
        session.commit()

    # Клиентът се отказва от поръчката
    r = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # Наличността на деактивирания продукт е възстановена (7 -> 10)
    with TestingSessionLocal() as session:
        refreshed_p = session.get(Product, product.id)
        assert refreshed_p is not None
        assert refreshed_p.active is False
        assert refreshed_p.stock == 10


def test_cancel_paid_order_returns_409(db_session: Session) -> None:
    """Критерий: Опит за отказ на платена поръчка връща 409 ORDER_ALREADY_PAID."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product, status="paid")

    r = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ORDER_ALREADY_PAID"


# ============================================================================
# 3. Директни проверки на DB ограниченията за PaymentAttempt
# ============================================================================


def test_payment_attempts_database_constraints(db_session: Session) -> None:
    """Критерий: UNIQUE(order_id, idempotency_key), частичен индекс и CHECK ограничения."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product)

    # 1. Валиден опит за плащане
    attempt1 = PaymentAttempt(
        order_id=order.id,
        payment_method="test_declined",
        status="declined",
        amount_minor=1000,
        currency="EUR",
        idempotency_key="key-db-1",
    )
    db_session.add(attempt1)
    db_session.commit()

    # 2. UNIQUE(order_id, idempotency_key) дубликат
    duplicate_key_attempt = PaymentAttempt(
        order_id=order.id,
        payment_method="test_success",
        status="succeeded",
        amount_minor=1000,
        currency="EUR",
        idempotency_key="key-db-1",
    )
    db_session.add(duplicate_key_attempt)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 3. Един успешен опит
    success_attempt = PaymentAttempt(
        order_id=order.id,
        payment_method="test_success",
        status="succeeded",
        amount_minor=1000,
        currency="EUR",
        idempotency_key="key-db-2",
    )
    db_session.add(success_attempt)
    db_session.commit()

    # 4. Частичен уникален индекс: втори успешен опит за същата поръчка се отхвърля
    second_success_attempt = PaymentAttempt(
        order_id=order.id,
        payment_method="test_success",
        status="succeeded",
        amount_minor=1000,
        currency="EUR",
        idempotency_key="key-db-3",
    )
    db_session.add(second_success_attempt)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 5. CHECK status IN ('succeeded', 'declined')
    invalid_status_attempt = PaymentAttempt(
        order_id=order.id,
        payment_method="test_success",
        status="pending",
        amount_minor=1000,
        currency="EUR",
        idempotency_key="key-db-4",
    )
    db_session.add(invalid_status_attempt)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 6. CHECK currency = 'EUR'
    invalid_currency_attempt = PaymentAttempt(
        order_id=order.id,
        payment_method="test_success",
        status="succeeded",
        amount_minor=1000,
        currency="USD",
        idempotency_key="key-db-5",
    )
    db_session.add(invalid_currency_attempt)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
