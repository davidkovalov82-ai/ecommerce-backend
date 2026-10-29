from fastapi.testclient import TestClient

from app.main import app

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
