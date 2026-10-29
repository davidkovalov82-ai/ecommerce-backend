from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_openapi_registered_operations() -> None:
    """Проверка чрез /openapi.json, че и трите основни операции са коректно регистрирани:

    1. GET /products (публичен списък с пагинация)
    2. GET /products/{product_id} (детайли за единичен продукт)
    3. POST /users (регистрация на нов потребител)
    """
    response = client.get("/openapi.json")
    assert response.status_code == 200

    schema = response.json()
    paths = schema.get("paths", {})

    # 1. GET /products
    assert "/products" in paths
    assert "get" in paths["/products"]
    assert paths["/products"]["get"]["operationId"] is not None

    # 2. GET /products/{product_id}
    assert "/products/{product_id}" in paths
    assert "get" in paths["/products/{product_id}"]
    assert paths["/products/{product_id}"]["get"]["operationId"] is not None

    # 3. POST /users
    assert "/users" in paths
    assert "post" in paths["/users"]
    assert paths["/users"]["post"]["operationId"] is not None
