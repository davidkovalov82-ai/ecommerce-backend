"""Тестове за основните сценарии на Catalog микросервиса."""

from fastapi.testclient import TestClient

from tests.conftest import make_auth_headers

INTERNAL_HEADERS = {"X-Internal-Token": "ecommerce-catalog-internal-secret"}


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
