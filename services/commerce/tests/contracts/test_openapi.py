from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_openapi_registered_operations() -> None:
    """Проверка чрез /openapi.json, че всички основни операции са коректно регистрирани:

    1. GET & POST /products
    2. GET & PATCH /products/{product_id}
    3. POST /users
    4. POST /auth/login
    5. GET /users/me
    6. GET /cart
    7. PUT & DELETE /cart/items/{product_id}
    8. GET & POST /orders
    9. GET /orders/{order_id}
    10. POST /orders/{order_id}/cancel
    11. POST /orders/{order_id}/checkout-session
    12. POST /webhooks/stripe
    """
    response = client.get("/openapi.json")
    assert response.status_code == 200

    schema = response.json()
    paths = schema.get("paths", {})

    # 1. GET & POST /products
    assert "/products" in paths
    assert "get" in paths["/products"]
    assert paths["/products"]["get"]["operationId"] is not None
    assert "post" in paths["/products"]
    assert paths["/products"]["post"]["operationId"] is not None

    # 2. GET & PATCH /products/{product_id}
    assert "/products/{product_id}" in paths
    assert "get" in paths["/products/{product_id}"]
    assert paths["/products/{product_id}"]["get"]["operationId"] is not None
    assert "patch" in paths["/products/{product_id}"]
    assert paths["/products/{product_id}"]["patch"]["operationId"] is not None

    # 3. POST /users
    assert "/users" in paths
    assert "post" in paths["/users"]
    assert paths["/users"]["post"]["operationId"] is not None

    # 4. POST /auth/login
    assert "/auth/login" in paths
    assert "post" in paths["/auth/login"]
    assert paths["/auth/login"]["post"]["operationId"] is not None

    # 5. GET /users/me
    assert "/users/me" in paths
    assert "get" in paths["/users/me"]
    assert paths["/users/me"]["get"]["operationId"] is not None

    # 6. GET /cart
    assert "/cart" in paths
    assert "get" in paths["/cart"]
    assert paths["/cart"]["get"]["operationId"] is not None

    # 7. PUT & DELETE /cart/items/{product_id}
    assert "/cart/items/{product_id}" in paths
    assert "put" in paths["/cart/items/{product_id}"]
    assert paths["/cart/items/{product_id}"]["put"]["operationId"] is not None
    assert "delete" in paths["/cart/items/{product_id}"]
    assert paths["/cart/items/{product_id}"]["delete"]["operationId"] is not None

    # 8. GET & POST /orders
    assert "/orders" in paths
    assert "get" in paths["/orders"]
    assert paths["/orders"]["get"]["operationId"] is not None
    assert "post" in paths["/orders"]
    assert paths["/orders"]["post"]["operationId"] is not None

    # 9. GET /orders/{order_id}
    assert "/orders/{order_id}" in paths
    assert "get" in paths["/orders/{order_id}"]
    assert paths["/orders/{order_id}"]["get"]["operationId"] is not None

    # Симулаторният endpoint за директно плащане е премахнат
    assert "/orders/{order_id}/payments" not in paths

    # 10. POST /orders/{order_id}/cancel
    assert "/orders/{order_id}/cancel" in paths
    assert "post" in paths["/orders/{order_id}/cancel"]
    assert paths["/orders/{order_id}/cancel"]["post"]["operationId"] is not None

    # 11. POST /orders/{order_id}/checkout-session
    assert "/orders/{order_id}/checkout-session" in paths
    assert "post" in paths["/orders/{order_id}/checkout-session"]
    assert (
        paths["/orders/{order_id}/checkout-session"]["post"]["operationId"] is not None
    )

    # 12. POST /webhooks/stripe
    assert "/webhooks/stripe" in paths
    assert "post" in paths["/webhooks/stripe"]
    assert paths["/webhooks/stripe"]["post"]["operationId"] is not None

    # 13. Bearer сигурностна схема в компонентите
    security_schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "HTTPBearer" in security_schemes
    assert security_schemes["HTTPBearer"]["type"] == "http"
    assert security_schemes["HTTPBearer"]["scheme"] == "bearer"
