"""Тестове за функционалността и контракта на Cart услугата."""

from fastapi.testclient import TestClient

from app.config import settings
from tests.conftest import MockCatalogClient, MockUsersClient, make_auth_headers


def test_get_empty_cart(client: TestClient) -> None:
    """Празният кош връща празен списък и 0 обща сума."""
    headers = make_auth_headers(user_id=10)
    resp = client.get("/cart", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total_minor"] == 0
    assert data["currency"] == "EUR"


def test_add_and_update_cart_item_does_not_reserve_stock(
    client: TestClient,
    mock_catalog: MockCatalogClient,
) -> None:
    """Добавянето в количката изчислява цени, но НЕ резервира складов остатък."""
    mock_catalog.set_mock_product(
        product_id=101,
        title="Wireless Mouse",
        price_minor=2500,
        currency="EUR",
        stock=5,
        active=True,
    )

    headers = make_auth_headers(user_id=10)

    # 1. Добавяне с количество 2
    resp = client.put("/cart/items/101", json={"quantity": 2}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["product_id"] == 101
    assert data["items"][0]["title"] == "Wireless Mouse"
    assert data["items"][0]["quantity"] == 2
    assert data["items"][0]["unit_price_minor"] == 2500
    assert data["items"][0]["subtotal_minor"] == 5000
    assert data["items"][0]["available"] is True
    assert data["total_minor"] == 5000

    # Проверка, че складовият остатък в Catalog не се е променил!
    catalog_product = mock_catalog.get_product(101)
    assert catalog_product is not None
    assert catalog_product.stock == 5

    # 2. Обновяване на количеството на 4
    resp_update = client.put("/cart/items/101", json={"quantity": 4}, headers=headers)
    assert resp_update.status_code == 200
    data_up = resp_update.json()
    assert data_up["items"][0]["quantity"] == 4
    assert data_up["total_minor"] == 10000

    # Складът отново е непокътнат
    assert mock_catalog.get_product(101).stock == 5


def test_add_to_cart_insufficient_stock_or_not_found(
    client: TestClient,
    mock_catalog: MockCatalogClient,
) -> None:
    """Грешки при липсващ продукт или недостатъчна наличност."""
    mock_catalog.set_mock_product(
        product_id=102,
        title="Keyboard",
        price_minor=4000,
        stock=3,
        active=True,
    )
    headers = make_auth_headers(user_id=11)

    # Несъществуващ продукт
    resp_404 = client.put("/cart/items/999", json={"quantity": 1}, headers=headers)
    assert resp_404.status_code == 404
    assert resp_404.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

    # Количество 5 при наличност 3
    resp_409 = client.put("/cart/items/102", json={"quantity": 5}, headers=headers)
    assert resp_409.status_code == 409
    assert resp_409.json()["detail"]["code"] == "INSUFFICIENT_STOCK"


def test_delete_cart_item(
    client: TestClient,
    mock_catalog: MockCatalogClient,
) -> None:
    """Премахване на позиция от количката."""
    mock_catalog.set_mock_product(product_id=103, stock=10)
    headers = make_auth_headers(user_id=12)

    client.put("/cart/items/103", json={"quantity": 1}, headers=headers)

    del_resp = client.delete("/cart/items/103", headers=headers)
    assert del_resp.status_code == 204

    get_resp = client.get("/cart", headers=headers)
    assert get_resp.json()["items"] == []


def test_auth_headers_without_jwt_returns_401(client: TestClient) -> None:
    """Заявки с фалшиви заглавки X-User-Id без JWT връщат 401."""
    # 1. GET /cart само с X-User-Id
    resp1 = client.get("/cart", headers={"X-User-Id": "10"})
    assert resp1.status_code == 401
    assert resp1.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"

    # 2. PUT /cart/items/{id} само с X-User-Id
    resp2 = client.put(
        "/cart/items/1",
        json={"quantity": 1},
        headers={"X-User-Id": "10"},
    )
    assert resp2.status_code == 401
    assert resp2.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"


def test_internal_cart_snapshot_and_clear_purchased_preserves_concurrent_changes(
    client: TestClient,
    mock_catalog: MockCatalogClient,
) -> None:
    """Вътрешен API за Orders: извличане на количка и частично изчистване на закупени стоки."""
    mock_catalog.set_mock_product(
        product_id=201, title="P1", price_minor=1000, stock=10
    )
    mock_catalog.set_mock_product(
        product_id=202, title="P2", price_minor=2000, stock=10
    )
    mock_catalog.set_mock_product(
        product_id=203, title="P3", price_minor=3000, stock=10
    )

    buyer_id = 42
    headers = make_auth_headers(user_id=buyer_id)
    internal_headers = {"X-Internal-Token": settings.internal_auth_token}

    # Потребителят добавя артикули 201 и 202
    client.put("/cart/items/201", json={"quantity": 1}, headers=headers)
    client.put("/cart/items/202", json={"quantity": 2}, headers=headers)

    # 1. Orders извлича вътрешна снимка на количката без токен -> 401
    unauth = client.get(f"/internal/cart/{buyer_id}")
    assert unauth.status_code == 401

    # Orders извлича вътрешна снимка с правилен X-Internal-Token
    snapshot_resp = client.get(f"/internal/cart/{buyer_id}", headers=internal_headers)
    assert snapshot_resp.status_code == 200
    items = snapshot_resp.json()["items"]
    assert len(items) == 2
    assert [i["product_id"] for i in items] == [201, 202]

    # Докато тече оформянето на поръчката, потребителят добавя нов артикул 203
    client.put("/cart/items/203", json={"quantity": 3}, headers=headers)

    # Orders потвърждава поръчката само за 201 и 202 с версиите от снимката
    clear_resp = client.post(
        f"/internal/cart/{buyer_id}/clear-purchased",
        headers=internal_headers,
        json={
            "order_id": 999,
            "items": [
                {"product_id": i["product_id"], "version": i["version"]} for i in items
            ],
        },
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["status"] == "cleared"
    assert clear_resp.json()["cleared_product_ids"] == [201, 202]

    # Проверка на количката: артикул 203 остава непокътнат!
    after_cart = client.get(
        f"/internal/cart/{buyer_id}", headers=internal_headers
    ).json()
    assert len(after_cart["items"]) == 1
    assert after_cart["items"][0]["product_id"] == 203
    assert after_cart["items"][0]["quantity"] == 3


def test_versioned_clear_and_idempotency_by_order_id(
    client: TestClient,
    mock_catalog: MockCatalogClient,
) -> None:
    """Проверка на K6.4: версионно изчистване и идемпотентност по order_id."""
    mock_catalog.set_mock_product(product_id=301, title="Item A", stock=20)
    mock_catalog.set_mock_product(product_id=302, title="Item B", stock=20)

    buyer_id = 55
    headers = make_auth_headers(user_id=buyer_id)
    internal_headers = {"X-Internal-Token": settings.internal_auth_token}

    # Потребителят добавя артикул 301 и 302
    client.put("/cart/items/301", json={"quantity": 2}, headers=headers)
    client.put("/cart/items/302", json={"quantity": 1}, headers=headers)

    # 1. Orders взима снимка на количката с версии
    snapshot = client.get(f"/internal/cart/{buyer_id}", headers=internal_headers).json()
    items = snapshot["items"]
    assert len(items) == 2
    item301 = next(i for i in items if i["product_id"] == 301)
    item302 = next(i for i in items if i["product_id"] == 302)
    v301 = item301["version"]
    v302 = item302["version"]
    assert v301 > 0
    assert v302 > 0

    # Потребителят променя количеството на 301 по време на checkout
    update_res = client.put("/cart/items/301", json={"quantity": 5}, headers=headers)
    assert update_res.status_code == 200

    # Orders прави изчистване за order_id=1001 с първоначалните версии от снимката
    clear_payload = {
        "order_id": 1001,
        "items": [
            {"product_id": 301, "version": v301},
            {"product_id": 302, "version": v302},
        ],
    }
    clear_res = client.post(
        f"/internal/cart/{buyer_id}/clear-purchased",
        headers=internal_headers,
        json=clear_payload,
    )
    assert clear_res.status_code == 200
    cleared_ids = clear_res.json()["cleared_product_ids"]
    # 301 не е изтрит, защото версията му е променена; 302 е успешно изтрит!
    assert 302 in cleared_ids
    assert 301 not in cleared_ids

    # Проверка на количката: 301 остава с новото количество 5 и по-нова версия!
    current_cart = client.get("/cart", headers=headers).json()
    assert len(current_cart["items"]) == 1
    assert current_cart["items"][0]["product_id"] == 301
    assert current_cart["items"][0]["quantity"] == 5
    assert current_cart["items"][0]["version"] > v301

    # Идемпотентност: повторен опит за същия order_id връща 200 без промени
    retry_clear = client.post(
        f"/internal/cart/{buyer_id}/clear-purchased",
        headers=internal_headers,
        json=clear_payload,
    )
    assert retry_clear.status_code == 200
    assert retry_clear.json()["status"] == "cleared"
    # Нищо не е изтрито втори път
    assert retry_clear.json()["cleared_product_ids"] == []


def test_cart_auth_revocation_blocking_and_fail_closed(
    client: TestClient,
    mock_users: MockUsersClient,
) -> None:
    """Проверка за валидиране на статус на потребителя през Users в Cart:

    1. Оттеглен токен (token_version mismatch) -> 401
    2. Блокиран потребител -> 403
    3. Недостъпен Users сервиз -> 503 fail-closed
    4. Празен или неконфигуриран X-Internal-Token -> 401
    """
    user_id = 99
    # 1. Оттеглен токен
    mock_users.set_user(user_id=user_id, token_version=2, status="active")
    stale_headers = make_auth_headers(user_id=user_id, token_version=0)
    resp = client.get("/cart", headers=stale_headers)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "INVALID_TOKEN"

    # 2. Блокиран потребител
    mock_users.set_user(user_id=user_id, token_version=0, status="blocked")
    valid_headers = make_auth_headers(user_id=user_id, token_version=0)
    resp = client.get("/cart", headers=valid_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "ACCOUNT_BLOCKED"

    # 3. Недостъпен Users сервиз -> 503
    mock_users.unavailable = True
    resp = client.get("/cart", headers=valid_headers)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "USERS_SERVICE_UNAVAILABLE"
    mock_users.unavailable = False

    # 4. Празен вътрешен токен
    resp = client.get(f"/internal/cart/{user_id}", headers={"X-Internal-Token": ""})
    assert resp.status_code == 401

    # Неконфигуриран вътрешен токен на сървъра
    orig_token = settings.internal_auth_token
    try:
        settings.internal_auth_token = ""
        resp = client.get(f"/internal/cart/{user_id}", headers={"X-Internal-Token": ""})
        assert resp.status_code == 401
    finally:
        settings.internal_auth_token = orig_token


def test_cart_readded_item_version_increment_and_safety(
    client: TestClient,
    mock_catalog: MockCatalogClient,
) -> None:
    """Изтриване и повторно добавяне на същия продукт получава нова по-голяма версия от sequence.

    Старата снимка не може да изтрие повторно добавения артикул.
    """
    mock_catalog.set_mock_product(product_id=401, stock=50)
    buyer_id = 66
    headers = make_auth_headers(user_id=buyer_id)
    internal_headers = {"X-Internal-Token": settings.internal_auth_token}

    # 1. Добавяме продукт 401
    client.put("/cart/items/401", json={"quantity": 1}, headers=headers)
    snap1 = client.get(f"/internal/cart/{buyer_id}", headers=internal_headers).json()
    v1 = snap1["items"][0]["version"]

    # 2. Изтриваме продукта от количката
    client.delete("/cart/items/401", headers=headers)
    assert client.get("/cart", headers=headers).json()["items"] == []

    # 3. Потребителят добавя същия продукт отново
    client.put("/cart/items/401", json={"quantity": 3}, headers=headers)
    snap2 = client.get(f"/internal/cart/{buyer_id}", headers=internal_headers).json()
    v2 = snap2["items"][0]["version"]

    # Версията строго нараства (чрез PostgreSQL sequence), не повтаря старата версия!
    assert v2 > v1

    # 4. Опит за изчистване със старата версия v1 за order_id=2001
    clear_res = client.post(
        f"/internal/cart/{buyer_id}/clear-purchased",
        headers=internal_headers,
        json={
            "order_id": 2001,
            "items": [{"product_id": 401, "version": v1}],
        },
    )
    assert clear_res.status_code == 200
    assert clear_res.json()["cleared_product_ids"] == []

    # Продуктът 401 с новото количество 3 остава непокътнат в количката!
    current_cart = client.get("/cart", headers=headers).json()
    assert len(current_cart["items"]) == 1
    assert current_cart["items"][0]["quantity"] == 3
