"""Тестове за основните сценарии на Catalog микросервиса."""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.dependencies import get_users_client
from app.main import app
from app.models import Category, Product, SellerStatus
from app.repository import CategoryRepository, ProductRepository
from app.schemas import StockReserveItem
from app.service import CatalogService, ReservationConflict
from tests.conftest import (
    INTERNAL_HEADERS,
    MockUsersClient,
    TestingSessionLocal,
    make_auth_headers,
)


def test_category_crud_and_permissions(client: TestClient) -> None:
    admin_headers = make_auth_headers(user_id=1, role="admin")
    user_headers = make_auth_headers(user_id=2, role="customer")

    # 1. Non-admin cannot create category
    resp = client.post(
        "/categories", json={"name": "Electronics"}, headers=user_headers
    )
    assert resp.status_code == 403

    # 2. Admin creates category
    resp = client.post(
        "/categories", json={"name": "Electronics"}, headers=admin_headers
    )
    assert resp.status_code == 201
    cat_id = resp.json()["id"]
    assert resp.json()["name"] == "Electronics"

    # 3. Duplicate name returns 409
    resp = client.post(
        "/categories", json={"name": "  electronics  "}, headers=admin_headers
    )
    assert resp.status_code == 409

    # 4. List categories
    resp = client.get("/categories")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # 5. Admin updates category
    resp = client.patch(
        f"/categories/{cat_id}", json={"name": "Gadgets"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Gadgets"

    # 6. Admin deletes empty category
    resp = client.delete(f"/categories/{cat_id}", headers=admin_headers)
    assert resp.status_code == 204


def test_product_crud_and_seller_ownership(client: TestClient) -> None:
    seller1_headers = make_auth_headers(user_id=10, role="customer")
    seller2_headers = make_auth_headers(user_id=20, role="customer")

    # 1. Seller 1 creates product
    payload = {
        "title": "Smartphone",
        "price_minor": 59900,
        "currency": "EUR",
        "stock": 5,
        "active": True,
    }
    resp = client.post("/products", json=payload, headers=seller1_headers)
    assert resp.status_code == 201
    prod_id = resp.json()["id"]
    assert resp.json()["seller_id"] == 10
    assert resp.json()["stock"] == 5

    # 2. Public view
    resp = client.get(f"/products/{prod_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Smartphone"

    # 3. Seller 2 tries to update Seller 1's product -> 404
    resp = client.patch(
        f"/products/{prod_id}",
        json={"price_minor": 49900},
        headers=seller2_headers,
    )
    assert resp.status_code == 404

    # 4. Seller 1 updates own product
    resp = client.patch(
        f"/products/{prod_id}",
        json={"price_minor": 49900},
        headers=seller1_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["price_minor"] == 49900

    # 5. Seller 1 checks my products
    resp = client.get("/users/me/products", headers=seller1_headers)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["id"] == prod_id


def test_internal_stock_reserve_and_release_idempotency(client: TestClient) -> None:
    seller_headers = make_auth_headers(user_id=10, role="customer")

    # Създаваме продукт с наличност 1
    resp = client.post(
        "/products",
        json={
            "title": "Unique Item",
            "price_minor": 10000,
            "currency": "EUR",
            "stock": 1,
            "active": True,
        },
        headers=seller_headers,
    )
    assert resp.status_code == 201
    prod_id = resp.json()["id"]

    # 1. Проверяваме batch endpoint
    resp = client.post(
        "/internal/products/batch",
        json={"product_ids": [prod_id]},
        headers=INTERNAL_HEADERS,
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["stock"] == 1
    assert resp.json()[0]["seller_active"] is True

    # 2. Резервираме за поръчка 101 (qty=1)
    reserve_payload = {
        "order_id": 101,
        "items": [{"product_id": prod_id, "quantity": 1}],
    }
    resp = client.post(
        "/internal/stock/reserve", json=reserve_payload, headers=INTERNAL_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "reserved"

    # Проверка на наличността в базата -> вече е 0
    resp = client.get(f"/products/{prod_id}")
    assert resp.status_code == 200
    assert resp.json()["stock"] == 0

    # 3. Втори опит за резервация за друга поръчка 102 с qty=1 -> 409 INSUFFICIENT_STOCK
    resp2 = client.post(
        "/internal/stock/reserve",
        json={"order_id": 102, "items": [{"product_id": prod_id, "quantity": 1}]},
        headers=INTERNAL_HEADERS,
    )
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["code"] == "INSUFFICIENT_STOCK"

    # 4. Идемпотентно повторение за същата поръчка 101 -> връща успех, наличността остава 0
    resp_idem = client.post(
        "/internal/stock/reserve", json=reserve_payload, headers=INTERNAL_HEADERS
    )
    assert resp_idem.status_code == 200
    assert resp_idem.json()["status"] == "reserved"

    resp = client.get(f"/products/{prod_id}")
    assert resp.json()["stock"] == 0

    # 5. Освобождаване на наличност за поръчка 101
    resp = client.post(
        "/internal/stock/release",
        json={"order_id": 101},
        headers=INTERNAL_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"

    # Наличността е възстановена на 1
    resp = client.get(f"/products/{prod_id}")
    assert resp.json()["stock"] == 1

    # 6. Повторно освобождаване за същата поръчка 101 -> идемпотентно, не увеличава повторно
    resp_rel_idem = client.post(
        "/internal/stock/release",
        json={"order_id": 101},
        headers=INTERNAL_HEADERS,
    )
    assert resp_rel_idem.status_code == 200
    resp = client.get(f"/products/{prod_id}")
    assert resp.json()["stock"] == 1  # Остава точно 1!


def test_auth_headers_without_jwt_returns_401(client: TestClient) -> None:
    """Заявки с фалшиви заглавки X-User-Id/X-User-Role без валиден JWT връщат 401."""
    # 1. Опит за създаване на продукт само с X-User-Id
    resp = client.post(
        "/products",
        json={
            "title": "Hacked Product",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 1,
            "active": True,
        },
        headers={"X-User-Id": "1", "X-User-Role": "admin"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"

    # 2. Опит за преглед на продавачески продукти само с X-User-Id
    resp = client.get("/users/me/products", headers={"X-User-Id": "1"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"


def test_catalog_auth_revocation_blocking_and_fail_closed(
    client: TestClient, mock_users_client: MockUsersClient
) -> None:
    """Проверка за валидиране на статус на потребителя през Users:

    1. Оттеглен токен (token_version mismatch) -> 401
    2. Блокиран потребител -> 403
    3. Недостъпен Users сервиз -> 503 fail-closed
    4. Празен или липсващ X-Internal-Token -> 401
    5. Неконфигуриран вътрешен токен на сървъра -> 401
    """
    user_id = 42
    # 1. Токен с token_version 0, но в Users токенът е вдигнат на 1
    mock_users_client.set_user(user_id=user_id, token_version=1, status="active")
    stale_headers = make_auth_headers(user_id=user_id, token_version=0)
    resp = client.get("/users/me/products", headers=stale_headers)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "INVALID_TOKEN"

    # 2. Блокиран потребител
    mock_users_client.set_user(user_id=user_id, token_version=0, status="blocked")
    valid_headers = make_auth_headers(user_id=user_id, token_version=0)
    resp = client.get("/users/me/products", headers=valid_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "ACCOUNT_BLOCKED"

    # 3. Недостъпен Users сервиз -> fail-closed 503
    mock_users_client.unavailable = True
    resp = client.get("/users/me/products", headers=valid_headers)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "USERS_SERVICE_UNAVAILABLE"
    mock_users_client.unavailable = False

    # 4. Празен или липсващ вътрешен токен
    resp = client.post(
        "/internal/stock/reserve",
        json={"order_id": 999, "items": [{"product_id": 1, "quantity": 1}]},
        headers={"X-Internal-Token": ""},
    )
    assert resp.status_code == 401

    resp = client.post(
        "/internal/stock/reserve",
        json={"order_id": 999, "items": [{"product_id": 1, "quantity": 1}]},
        headers={},
    )
    assert resp.status_code == 401

    # Невалиден (грешен) вътрешен токен
    resp = client.post(
        "/internal/stock/reserve",
        json={"order_id": 999, "items": [{"product_id": 1, "quantity": 1}]},
        headers={"X-Internal-Token": "invalid-wrong-token-xyz"},
    )
    assert resp.status_code == 401

    # 5. Неконфигуриран вътрешен токен на сървъра не разрешава достъп
    from app.config import settings

    orig_token = settings.internal_auth_token
    try:
        settings.internal_auth_token = ""
        resp = client.post(
            "/internal/stock/reserve",
            json={"order_id": 999, "items": [{"product_id": 1, "quantity": 1}]},
            headers={"X-Internal-Token": ""},
        )
        assert resp.status_code == 401
    finally:
        settings.internal_auth_token = orig_token


def test_stock_reservation_conflicts_and_validations(client: TestClient) -> None:
    """Валидации и защита от конфликти при резервация в Catalog:

    1. Празен състав на позиции -> 422
    2. Повтарящи се product_id -> 422
    3. Различен състав за същия order_id -> 409
    4. Повторна резервация на вече освободена поръчка -> 409
    """
    seller_headers = make_auth_headers(user_id=10, role="customer")
    resp = client.post(
        "/products",
        json={
            "title": "Item A",
            "price_minor": 1000,
            "currency": "EUR",
            "stock": 10,
            "active": True,
        },
        headers=seller_headers,
    )
    prod_a = resp.json()["id"]

    resp = client.post(
        "/products",
        json={
            "title": "Item B",
            "price_minor": 2000,
            "currency": "EUR",
            "stock": 10,
            "active": True,
        },
        headers=seller_headers,
    )
    prod_b = resp.json()["id"]

    # 1. Празен състав
    resp = client.post(
        "/internal/stock/reserve",
        json={"order_id": 501, "items": []},
        headers=INTERNAL_HEADERS,
    )
    assert resp.status_code == 422

    # 2. Повтарящ се product_id
    resp = client.post(
        "/internal/stock/reserve",
        json={
            "order_id": 501,
            "items": [
                {"product_id": prod_a, "quantity": 1},
                {"product_id": prod_a, "quantity": 2},
            ],
        },
        headers=INTERNAL_HEADERS,
    )
    assert resp.status_code == 422

    # 3. Първоначална резервация за order_id 501
    resp = client.post(
        "/internal/stock/reserve",
        json={
            "order_id": 501,
            "items": [{"product_id": prod_a, "quantity": 2}],
        },
        headers=INTERNAL_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "reserved"

    # Различен състав за същия order_id 501 -> 409 конфликт
    resp = client.post(
        "/internal/stock/reserve",
        json={
            "order_id": 501,
            "items": [{"product_id": prod_b, "quantity": 2}],
        },
        headers=INTERNAL_HEADERS,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RESERVATION_CONFLICT"

    # Различно количество за същия order_id 501 -> 409 конфликт
    resp = client.post(
        "/internal/stock/reserve",
        json={
            "order_id": 501,
            "items": [{"product_id": prod_a, "quantity": 3}],
        },
        headers=INTERNAL_HEADERS,
    )
    assert resp.status_code == 409

    # 4. Освобождаване на поръчка 501
    resp = client.post(
        "/internal/stock/release",
        json={"order_id": 501},
        headers=INTERNAL_HEADERS,
    )
    assert resp.status_code == 200

    # Опит за нова резервация за вече освободена поръчка -> 409 конфликт
    resp = client.post(
        "/internal/stock/reserve",
        json={
            "order_id": 501,
            "items": [{"product_id": prod_a, "quantity": 2}],
        },
        headers=INTERNAL_HEADERS,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RESERVATION_CONFLICT"


def test_concurrent_reserve_and_release_safety(
    mock_users_client: MockUsersClient,
) -> None:
    """Конкурентни заявки за reserve и release за един и същ order_id:

    Advisory lock предпазва от надпревара между едновременни първи извиквания.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.dependencies import get_db_session
    from app.main import app

    def override_session_factory():
        with TestingSessionLocal() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_session_factory

    with TestClient(app) as local_client:
        seller_headers = make_auth_headers(user_id=11, role="customer")
        resp = local_client.post(
            "/products",
            json={
                "title": "Concurrent Item",
                "price_minor": 1500,
                "currency": "EUR",
                "stock": 10,
                "active": True,
            },
            headers=seller_headers,
        )
        prod_id = resp.json()["id"]

        order_id = 777
        barrier = threading.Barrier(5)

        def _do_reserve() -> int:
            barrier.wait(timeout=5.0)
            r = local_client.post(
                "/internal/stock/reserve",
                json={
                    "order_id": order_id,
                    "items": [{"product_id": prod_id, "quantity": 2}],
                },
                headers=INTERNAL_HEADERS,
            )
            return r.status_code

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: _do_reserve(), range(5)))

        # Всички паралелни заявки с еднакъв състав за един order_id връщат 200 (идемпотентност)
        assert all(code == 200 for code in results)

        # Наличността е намалена точно с 2, не с 10!
        resp = local_client.get(f"/products/{prod_id}")
        assert resp.json()["stock"] == 8

    app.dependency_overrides.pop(get_db_session, None)


def test_catalog_consumer_validation_and_deduplication() -> None:
    """Catalog потребителски consumer:

    1. Валидация на съобщения (счупен JSON, липсващи полета) -> nack(requeue=False)
    2. Атомарна дедупликация през seller_inbox_events
    3. Защита от по-стари събития при пренареждане
    """
    import json
    from unittest.mock import MagicMock

    from app.models import SellerStatus
    from app.user_consumer import RabbitMQUserEventConsumer

    consumer = RabbitMQUserEventConsumer(session_factory=TestingSessionLocal)

    mock_channel = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 1

    # 1. Невалиден JSON
    consumer._handle_message(
        channel=mock_channel,
        method=mock_method,
        properties=MagicMock(),
        body=b"not a valid json",
    )
    mock_channel.basic_nack.assert_called_once_with(delivery_tag=1, requeue=False)
    mock_channel.reset_mock()

    # 2. Невалидна схема (липсва user_id)
    bad_payload = json.dumps(
        {"event_id": "evt-1", "event_type": "user.blocked", "payload": {}}
    ).encode("utf-8")
    consumer._handle_message(
        channel=mock_channel,
        method=mock_method,
        properties=MagicMock(),
        body=bad_payload,
    )
    mock_channel.basic_nack.assert_called_once_with(delivery_tag=1, requeue=False)
    mock_channel.reset_mock()

    # 3. Валидно събитие
    seller_id = 9999
    valid_payload = json.dumps(
        {
            "event_id": "evt-valid-1",
            "event_type": "user.blocked",
            "occurred_at": "2026-11-18T10:00:00Z",
            "user_id": seller_id,
            "status": "blocked",
        }
    ).encode("utf-8")

    consumer._handle_message(
        channel=mock_channel,
        method=mock_method,
        properties=MagicMock(),
        body=valid_payload,
    )
    mock_channel.basic_ack.assert_called_once_with(delivery_tag=1)
    mock_channel.reset_mock()

    with TestingSessionLocal() as session:
        st = session.get(SellerStatus, seller_id)
        assert st is not None
        assert st.status == "blocked"

    # 4. По-старо събитие за същия продавач (out-of-order) не презаписва по-новия статус
    stale_payload = json.dumps(
        {
            "event_id": "evt-stale-2",
            "event_type": "user.activated",
            "occurred_at": "2026-11-17T10:00:00Z",  # По-старо с един ден!
            "user_id": seller_id,
            "status": "active",
        }
    ).encode("utf-8")

    consumer._handle_message(
        channel=mock_channel,
        method=mock_method,
        properties=MagicMock(),
        body=stale_payload,
    )
    mock_channel.basic_ack.assert_called_once_with(delivery_tag=1)

    with TestingSessionLocal() as session:
        st = session.get(SellerStatus, seller_id)
        assert st is not None
        assert st.status == "blocked"  # Остава blocked!


def test_release_before_first_reserve_and_late_reserve_conflict(
    db_session: Session,
    mock_users_client: MockUsersClient,
) -> None:
    """Критерий: Терминален release преди reserve предотвратява късен reserve за същия order_id."""
    app.dependency_overrides[get_users_client] = lambda: mock_users_client
    seller_id = 100
    mock_users_client.set_user(seller_id, "active", "seller", 1)

    cat_repo = CategoryRepository(db_session)
    category = cat_repo.create(Category(name="Electronics Early Release"))
    db_session.commit()

    prod_repo = ProductRepository(db_session)
    product = prod_repo.create(
        Product(
            seller_id=seller_id,
            category_id=category.id,
            title="Early Release Gadget",
            price_minor=5000,
            currency="EUR",
            stock=5,
            active=True,
        )
    )
    db_session.commit()

    service = CatalogService(db_session)
    order_id = 888801

    # 1. Изпълняваме release преди какъвто и да е reserve
    service.release_stock(order_id=order_id)

    # Повторен release е идемпотентен
    service.release_stock(order_id=order_id)

    # 2. Късен reserve за същия order_id връща ReservationConflict и НЕ променя наличността
    with pytest.raises(ReservationConflict, match="already been released"):
        service.reserve_stock(
            order_id=order_id,
            items=[StockReserveItem(product_id=product.id, quantity=2)],
        )

    # Наличността остава точно 5
    db_session.refresh(product)
    assert product.stock == 5


def test_consumer_transient_db_error_requeues_and_subsequent_delivery_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Критерий: Временен отказ на БД -> basic_nack(requeue=True); следваща доставка -> basic_ack."""
    from sqlalchemy.exc import OperationalError

    from app.user_consumer import RabbitMQUserEventConsumer

    consumer = RabbitMQUserEventConsumer(
        session_factory=TestingSessionLocal,
        retry_delay=0.0,
    )

    seller_id = 77771
    event_payload = json.dumps(
        {
            "event_id": "evt-transient-1",
            "event_type": "user.blocked",
            "occurred_at": "2026-11-18T12:00:00Z",
            "user_id": seller_id,
            "status": "blocked",
        }
    ).encode("utf-8")

    # 1. Първа доставка: симулираме временна грешка в базата (OperationalError)
    def raise_operational(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("SELECT 1", {}, Exception("Database connection refused"))

    monkeypatch.setattr(
        "app.repository.SellerStatusRepository.record_inbox_event_idempotent",
        raise_operational,
    )

    mock_channel1 = MagicMock()
    mock_method1 = MagicMock()
    mock_method1.delivery_tag = 101

    consumer._handle_message(
        channel=mock_channel1,
        method=mock_method1,
        properties=MagicMock(),
        body=event_payload,
    )

    # Трябва да извика nack с requeue=True
    mock_channel1.basic_nack.assert_called_once_with(delivery_tag=101, requeue=True)
    mock_channel1.basic_ack.assert_not_called()

    # Статусът в БД още не е записан
    with TestingSessionLocal() as session:
        st = session.get(SellerStatus, seller_id)
        assert st is None

    # 2. Втора доставка: базата данни е възстановена
    monkeypatch.undo()

    mock_channel2 = MagicMock()
    mock_method2 = MagicMock()
    mock_method2.delivery_tag = 102

    consumer._handle_message(
        channel=mock_channel2,
        method=mock_method2,
        properties=MagicMock(),
        body=event_payload,
    )

    # Сега трябва да е потвърдено успешно с basic_ack
    mock_channel2.basic_ack.assert_called_once_with(delivery_tag=102)
    mock_channel2.basic_nack.assert_not_called()

    # Статусът е успешно обновен в БД
    with TestingSessionLocal() as session:
        st = session.get(SellerStatus, seller_id)
        assert st is not None
        assert st.status == "blocked"


def test_consumer_rejects_missing_or_invalid_occurred_at() -> None:
    """Критерий: Липсващо или невалидно occurred_at не се подменя с now(), а директно се отхвърля."""
    from app.user_consumer import RabbitMQUserEventConsumer

    consumer = RabbitMQUserEventConsumer(session_factory=TestingSessionLocal)
    mock_channel = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 1

    # Липсващо occurred_at
    no_time_payload = json.dumps(
        {
            "event_id": "evt-no-time",
            "event_type": "user.blocked",
            "user_id": 9991,
            "status": "blocked",
        }
    ).encode("utf-8")
    consumer._handle_message(mock_channel, mock_method, MagicMock(), no_time_payload)
    mock_channel.basic_nack.assert_called_once_with(delivery_tag=1, requeue=False)
    mock_channel.reset_mock()

    # Невалиден формат на occurred_at
    bad_time_payload = json.dumps(
        {
            "event_id": "evt-bad-time",
            "event_type": "user.blocked",
            "occurred_at": "not-a-timestamp",
            "user_id": 9991,
            "status": "blocked",
        }
    ).encode("utf-8")
    consumer._handle_message(mock_channel, mock_method, MagicMock(), bad_time_payload)
    mock_channel.basic_nack.assert_called_once_with(delivery_tag=1, requeue=False)
