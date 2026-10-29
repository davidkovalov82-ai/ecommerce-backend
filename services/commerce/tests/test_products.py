from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.main import app
from app.modules.catalog.models import Product

client = TestClient(app)


def test_get_existing_product() -> None:
    """Критерий 1: GET /products/1 връща 200 и шестте полета на продукта."""
    response = client.get("/products/1")
    assert response.status_code == 200
    assert response.json() == {
        "id": 1,
        "title": "Notebook",
        "price_minor": 1299,
        "currency": "EUR",
        "stock": 10,
        "active": True,
    }


def test_get_missing_product() -> None:
    """Критерий 2: GET /products/999 връща 404 с договорената структура на detail."""
    response = client.get("/products/999")
    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "PRODUCT_NOT_FOUND",
            "message": "Product not found",
        }
    }


def test_get_invalid_product_id() -> None:
    """Критерий 3: GET /products/abc връща 422 Unprocessable Entity."""
    response = client.get("/products/abc")
    assert response.status_code == 422


def test_list_active_products(db_session: Session) -> None:
    """Тест 1: GET /products връща списък с активни продукти и пълната им структура."""
    p1 = Product(
        title="Wireless Mouse",
        price_minor=2500,
        currency="EUR",
        stock=15,
        active=True,
    )
    p2 = Product(
        title="Mechanical Keyboard",
        price_minor=8900,
        currency="EUR",
        stock=8,
        active=True,
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    response = client.get("/products")
    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 20
    assert data["offset"] == 0

    items = data["items"]
    assert len(items) >= 3  # id=1 (Notebook) + 2 нови продукта
    titles = [item["title"] for item in items]
    assert "Notebook" in titles
    assert "Wireless Mouse" in titles
    assert "Mechanical Keyboard" in titles

    for item in items:
        assert item["active"] is True
        assert isinstance(item["id"], int)
        assert isinstance(item["title"], str)
        assert isinstance(item["price_minor"], int)
        assert item["currency"] == "EUR"
        assert isinstance(item["stock"], int)


def test_list_products_excludes_inactive(db_session: Session) -> None:
    """Тест 2: GET /products изключва неактивните продукти от резултата."""
    active_prod = Product(
        title="Active Monitor",
        price_minor=19900,
        currency="EUR",
        stock=5,
        active=True,
    )
    inactive_prod = Product(
        title="Archived Monitor",
        price_minor=14900,
        currency="EUR",
        stock=0,
        active=False,
    )
    db_session.add_all([active_prod, inactive_prod])
    db_session.commit()

    response = client.get("/products")
    assert response.status_code == 200
    data = response.json()
    returned_ids = [item["id"] for item in data["items"]]
    returned_titles = [item["title"] for item in data["items"]]

    assert active_prod.id in returned_ids
    assert "Active Monitor" in returned_titles
    assert inactive_prod.id not in returned_ids
    assert "Archived Monitor" not in returned_titles
    assert all(item["active"] is True for item in data["items"])


def test_list_products_ordering_and_pagination(db_session: Session) -> None:
    """Тест 3: GET /products спазва възходящо подреждане по ID и коректни limit и offset."""
    prods = [
        Product(
            title=f"Catalog Item {i}",
            price_minor=1000 * i,
            currency="EUR",
            stock=i,
            active=True,
        )
        for i in range(1, 6)
    ]
    db_session.add_all(prods)
    db_session.commit()

    # 1. Проверка на възходящо подреждане по ID
    full_response = client.get("/products?limit=50&offset=0")
    assert full_response.status_code == 200
    full_items = full_response.json()["items"]
    ids = [item["id"] for item in full_items]
    assert ids == sorted(ids)

    # 2. Проверка на limit
    limit_response = client.get("/products?limit=2&offset=0")
    assert limit_response.status_code == 200
    limit_data = limit_response.json()
    assert limit_data["limit"] == 2
    assert limit_data["offset"] == 0
    assert len(limit_data["items"]) == 2
    assert limit_data["items"] == full_items[:2]

    # 3. Проверка на offset
    offset_response = client.get("/products?limit=2&offset=2")
    assert offset_response.status_code == 200
    offset_data = offset_response.json()
    assert offset_data["limit"] == 2
    assert offset_data["offset"] == 2
    assert len(offset_data["items"]) == 2
    assert offset_data["items"] == full_items[2:4]


def test_list_products_empty_result(db_session: Session) -> None:
    """Тест 4: GET /products връща 200 с items=[] при липса на съвпадащи продукти."""
    # Вариант А: offset извън обхвата на съществуващите записи
    offset_response = client.get("/products?offset=500")
    assert offset_response.status_code == 200
    assert offset_response.json() == {
        "items": [],
        "limit": 20,
        "offset": 500,
    }

    # Вариант Б: няма активни продукти в базата
    db_session.execute(text("UPDATE products SET active = false WHERE active = true"))
    db_session.commit()
    try:
        empty_response = client.get("/products")
        assert empty_response.status_code == 200
        assert empty_response.json() == {
            "items": [],
            "limit": 20,
            "offset": 0,
        }
    finally:
        db_session.execute(text("UPDATE products SET active = true WHERE id = 1"))
        db_session.commit()


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": -1},
        {"limit": "abc"},
        {"offset": -1},
        {"offset": "xyz"},
    ],
)
def test_list_products_invalid_parameters(params: dict[str, Any]) -> None:
    """Тест 5: Невалидните параметри за limit и offset връщат 422 Unprocessable Entity."""
    response = client.get("/products", params=params)
    assert response.status_code == 422
