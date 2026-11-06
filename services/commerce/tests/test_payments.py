import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

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
    # Симулираме, че наличността вече е намалена при checkout
    product.stock -= quantity
    db_session.commit()
    db_session.refresh(order)
    return order


# ============================================================================
# 1. Автентикация и валидация на входа
# ============================================================================


def test_payment_and_cancel_auth_required() -> None:
    """Критерий: Без автентикация двата endpoint-а връщат 401."""
    # POST /orders/1/payments
    r_pay = client.post(
        "/orders/1/payments",
        json={"payment_method": "test_success"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r_pay.status_code == 401

    # POST /orders/1/cancel
    r_cancel = client.post("/orders/1/cancel")
    assert r_cancel.status_code == 401


def test_payment_missing_or_invalid_idempotency_key(db_session: Session) -> None:
    """Критерий: Липсващ или невалиден Idempotency-Key връща 422."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product)

    # Липсващ header
    r_missing = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_success"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_missing.status_code == 422

    # Невалиден UUID
    r_invalid = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "not-a-valid-uuid",
        },
    )
    assert r_invalid.status_code == 422


def test_payment_forbids_extra_fields_and_unsupported_methods(
    db_session: Session,
) -> None:
    """Критерий: Забранени допълнителни полета (сума, карта) и неподдържани методи връщат 422."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product)

    key = str(uuid.uuid4())

    # Допълнително поле amount
    r_extra = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_success", "amount_minor": 100},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key,
        },
    )
    assert r_extra.status_code == 422

    # Неподдържан payment_method
    r_unsupported = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "credit_card"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key,
        },
    )
    assert r_unsupported.status_code == 422


def test_payment_and_cancel_foreign_or_missing_order_returns_404(
    db_session: Session,
) -> None:
    """Критерий: Чужда или несъществуваща поръчка връща идентичен 404 ORDER_NOT_FOUND."""
    user1, token1 = _create_user_and_token(db_session, email="u1@example.com")
    _user2, token2 = _create_user_and_token(db_session, email="u2@example.com")
    product = _create_product(db_session)
    order1 = _create_order(db_session, user1.id, product)

    # Несъществуваща поръчка
    non_existent_id = 999999
    r_pay_non_existent = client.post(
        f"/orders/{non_existent_id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token1}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert r_pay_non_existent.status_code == 404
    assert r_pay_non_existent.json()["detail"]["code"] == "ORDER_NOT_FOUND"

    r_cancel_non_existent = client.post(
        f"/orders/{non_existent_id}/cancel",
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert r_cancel_non_existent.status_code == 404
    assert r_cancel_non_existent.json()["detail"]["code"] == "ORDER_NOT_FOUND"

    # Чужда поръчка
    r_pay_foreign = client.post(
        f"/orders/{order1.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token2}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert r_pay_foreign.status_code == 404
    assert r_pay_foreign.json()["detail"]["code"] == "ORDER_NOT_FOUND"

    r_cancel_foreign = client.post(
        f"/orders/{order1.id}/cancel",
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert r_cancel_foreign.status_code == 404
    assert r_cancel_foreign.json()["detail"]["code"] == "ORDER_NOT_FOUND"


# ============================================================================
# 2. Успешно плащане и идемпотентност
# ============================================================================


def test_successful_payment_flow_and_replay(db_session: Session) -> None:
    """Критерий: Успех (pending_payment -> paid), stock непроменен, replay със същия ключ връща 200."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10)
    order = _create_order(db_session, user.id, product, quantity=3)  # stock стана 7
    initial_stock = product.stock
    assert initial_stock == 7

    key = str(uuid.uuid4())

    # 1. Първи успешен опит -> 201 Created
    r = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key,
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["order_id"] == order.id
    assert data["status"] == "succeeded"
    assert data["amount_minor"] == order.total_minor
    assert data["currency"] == "EUR"
    assert "id" in data

    # Проверка в базата: поръчката е paid, stock НЕ е намален отново
    with TestingSessionLocal() as session:
        db_order = session.get(Order, order.id)
        assert db_order is not None
        assert db_order.status == "paid"

        db_product = session.get(Product, product.id)
        assert db_product is not None
        assert db_product.stock == 7  # непроменен

        attempts = (
            session.query(PaymentAttempt)
            .filter(PaymentAttempt.order_id == order.id)
            .all()
        )
        assert len(attempts) == 1
        assert attempts[0].status == "succeeded"

    # 2. Идемпотентен replay със същата поръчка + същия ключ + същия метод -> 200 OK
    r_replay = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key,
        },
    )
    assert r_replay.status_code == 200
    replay_data = r_replay.json()
    assert replay_data["id"] == data["id"]
    assert replay_data["status"] == "succeeded"

    # Броят опити в БД си остава 1
    with TestingSessionLocal() as session:
        attempts = (
            session.query(PaymentAttempt)
            .filter(PaymentAttempt.order_id == order.id)
            .all()
        )
        assert len(attempts) == 1


def test_declined_payment_replay_and_retry_with_new_key(db_session: Session) -> None:
    """Критерий: Declined -> replay със същия ключ (200) -> успех с нов ключ (201):

    Общо два записа за опити, само един успешен, поръчката става paid.
    """
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product)

    key1 = str(uuid.uuid4())

    # 1. Неуспешен опит -> 201 Created (записан опит със статус declined)
    r_declined = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_declined"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key1,
        },
    )
    assert r_declined.status_code == 201
    declined_data = r_declined.json()
    assert declined_data["status"] == "declined"
    attempt1_id = declined_data["id"]

    # Поръчката остава pending_payment
    with TestingSessionLocal() as session:
        db_order = session.get(Order, order.id)
        assert db_order is not None
        assert db_order.status == "pending_payment"

    # 2. Повторение със същия ключ и същия метод -> 200 OK, връща същия declined опит
    r_replay = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_declined"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key1,
        },
    )
    assert r_replay.status_code == 200
    assert r_replay.json()["id"] == attempt1_id
    assert r_replay.json()["status"] == "declined"

    # 3. Нов опит с нов ключ и test_success -> 201 Created
    key2 = str(uuid.uuid4())
    r_success = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key2,
        },
    )
    assert r_success.status_code == 201
    success_data = r_success.json()
    assert success_data["status"] == "succeeded"

    # Финално състояние в БД: общо 2 опита, 1 succeeded, поръчката е paid
    with TestingSessionLocal() as session:
        db_order = session.get(Order, order.id)
        assert db_order is not None
        assert db_order.status == "paid"

        attempts = (
            session.query(PaymentAttempt)
            .filter(PaymentAttempt.order_id == order.id)
            .order_by(PaymentAttempt.id.asc())
            .all()
        )
        assert len(attempts) == 2
        assert attempts[0].id == attempt1_id
        assert attempts[0].status == "declined"
        assert attempts[1].status == "succeeded"


def test_idempotency_conflict_with_different_method(db_session: Session) -> None:
    """Критерий: Същият ключ за същата поръчка с променен payment_method връща 409 IDEMPOTENCY_CONFLICT."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product)

    key = str(uuid.uuid4())

    # 1. Първи опит с test_declined
    r1 = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_declined"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key,
        },
    )
    assert r1.status_code == 201

    # 2. Втори опит със същия ключ, но test_success -> 409
    r2 = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key,
        },
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_new_payment_attempt_on_paid_or_cancelled_order_returns_409(
    db_session: Session,
) -> None:
    """Критерий: Нов опит (с нов ключ) за paid или cancelled поръчка връща 409."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session)

    # 1. Поръчка, която вече е paid
    paid_order = _create_order(db_session, user.id, product, status="paid")
    r_pay_paid = client.post(
        f"/orders/{paid_order.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert r_pay_paid.status_code == 409
    assert r_pay_paid.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"

    # 2. Поръчка, която е cancelled
    cancelled_order = _create_order(db_session, user.id, product, status="cancelled")
    r_pay_cancelled = client.post(
        f"/orders/{cancelled_order.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert r_pay_cancelled.status_code == 409
    assert r_pay_cancelled.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"


# ============================================================================
# 3. Отказ от поръчка (Cancellation) и възстановяване на stock
# ============================================================================


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
# 4. Конкурентност (Concurrency & Race Conditions)
# ============================================================================


def test_concurrent_payments_with_different_keys(db_session: Session) -> None:
    """Критерий: Две едновременни плащания с различни ключове за една поръчка:

    Само едно успява (201), а другото получава 409 (защото поръчката вече е платена).
    """
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10)
    order = _create_order(db_session, user.id, product, quantity=2)

    barrier = threading.Barrier(2)

    def _pay(key: str) -> int:
        barrier.wait(timeout=5.0)
        r = client.post(
            f"/orders/{order.id}/payments",
            json={"payment_method": "test_success"},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": key,
            },
        )
        return r.status_code

    key1 = str(uuid.uuid4())
    key2 = str(uuid.uuid4())

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_pay, key1)
        f2 = executor.submit(_pay, key2)
        status1 = f1.result(timeout=10.0)
        status2 = f2.result(timeout=10.0)

    statuses = sorted([status1, status2])
    assert statuses == [201, 409]

    # Крайно състояние: само един успешен опит в БД и поръчката е paid
    with TestingSessionLocal() as session:
        db_order = session.get(Order, order.id)
        assert db_order is not None
        assert db_order.status == "paid"

        attempts = (
            session.query(PaymentAttempt)
            .filter(PaymentAttempt.order_id == order.id)
            .all()
        )
        assert len(attempts) == 1
        assert attempts[0].status == "succeeded"


def test_concurrent_payment_and_cancel_race_condition(db_session: Session) -> None:
    """Критерий: Едновременно успешно плащане и отказ:

    Или поръчката е paid и бройките остават резервирани,
    или е cancelled и са върнати.
    НИКОГА paid с върнати бройки! Проверява се крайното състояние в БД.
    """
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock=8
    assert product.stock == 8

    barrier = threading.Barrier(2)

    def _do_payment() -> int:
        barrier.wait(timeout=5.0)
        r = client.post(
            f"/orders/{order.id}/payments",
            json={"payment_method": "test_success"},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        return r.status_code

    def _do_cancel() -> int:
        barrier.wait(timeout=5.0)
        r = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        return r.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_pay = executor.submit(_do_payment)
        f_cancel = executor.submit(_do_cancel)
        status_pay = f_pay.result(timeout=10.0)
        status_cancel = f_cancel.result(timeout=10.0)

    # Проверка на резултатите от двата паралелни заявки:
    # Едната трябва да успее (201 или 200), а другата да бъде отхвърлена с 409 конфликт
    assert {status_pay, status_cancel} == {201, 409} or {
        status_pay,
        status_cancel,
    } == {200, 409}

    # Проверка на инварианта в базата данни
    with TestingSessionLocal() as session:
        final_order = session.get(Order, order.id)
        final_product = session.get(Product, product.id)
        assert final_order is not None
        assert final_product is not None

        if final_order.status == "paid":
            # Плащането е спечелило: отказът е получил 409
            assert status_pay == 201
            assert status_cancel == 409
            # Наличността НЕ трябва да е връщана (остава 8)
            assert final_product.stock == 8
        elif final_order.status == "cancelled":
            # Отказът е спечелил: плащането е получило 409
            assert status_cancel == 200
            assert status_pay == 409
            # Наличността задължително е върната (става 10)
            assert final_product.stock == 10
        else:
            pytest.fail(f"Неочакван краен статус на поръчката: {final_order.status}")


# ============================================================================
# 5. Директни проверки на DB ограниченията за PaymentAttempt
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


def test_rollback_on_service_exception_before_commit(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Критерий: При възникване на грешка преди commit транзакцията се връща назад (rollback)."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product)

    from app.modules.orders.repository import OrderRepository

    def _buggy_add_payment_attempt(self, attempt: PaymentAttempt) -> PaymentAttempt:
        raise RuntimeError("Simulated database failure before commit")

    monkeypatch.setattr(
        OrderRepository, "add_payment_attempt", _buggy_add_payment_attempt
    )

    with pytest.raises(RuntimeError):
        client.post(
            f"/orders/{order.id}/payments",
            json={"payment_method": "test_success"},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )

    # Проверка: поръчката остава pending_payment и няма записани опити
    with TestingSessionLocal() as session:
        db_order = session.get(Order, order.id)
        assert db_order is not None
        assert db_order.status == "pending_payment"

        attempts = (
            session.query(PaymentAttempt)
            .filter(PaymentAttempt.order_id == order.id)
            .all()
        )
        assert len(attempts) == 0
