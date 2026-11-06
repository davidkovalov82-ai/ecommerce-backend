from fastapi.testclient import TestClient
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


def test_get_inactive_product_returns_404(db_session: Session) -> None:
    """Публичният детайл по ID връща 404 за неактивен продукт."""
    inactive_product = Product(
        title="Archived Old Product",
        price_minor=1999,
        currency="EUR",
        stock=0,
        active=False,
    )
    db_session.add(inactive_product)
    db_session.commit()
    db_session.refresh(inactive_product)

    response = client.get(f"/products/{inactive_product.id}")
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
