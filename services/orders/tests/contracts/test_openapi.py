from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_openapi_registered_operations() -> None:
    """Проверка чрез /openapi.json на Orders API:

    1. Чуждите пътища не са регистрирани в Orders: /products, /categories, /users, /auth, /cart
    2. Всички основни операции на поръчките и доставките са регистрирани:
       - GET & POST /orders
       - GET /orders/{order_id}
       - POST /orders/{order_id}/cancel
       - POST /orders/{order_id}/checkout-session
       - GET /seller/shipments
       - GET /seller/shipments/{shipment_id}
       - POST /seller/shipments/{shipment_id}/ship
    3. Bearer HTTP сигурностна схема
    """
    response = client.get("/openapi.json")
    assert response.status_code == 200

    schema = response.json()
    paths = schema.get("paths", {})

    # 1. Чужди домейни са изнесени и НЕ присъстват в Orders API
    assert "/products" not in paths
    assert "/categories" not in paths
    assert "/users" not in paths
    assert "/auth/login" not in paths
    assert "/cart" not in paths
    assert "/webhooks/stripe" not in paths
    assert "/orders/{order_id}/payments" not in paths

    # 2. GET & POST /orders
    assert "/orders" in paths
    assert "get" in paths["/orders"]
    assert paths["/orders"]["get"]["operationId"] is not None
    assert "post" in paths["/orders"]
    assert paths["/orders"]["post"]["operationId"] is not None

    # 3. GET /orders/{order_id}
    assert "/orders/{order_id}" in paths
    assert "get" in paths["/orders/{order_id}"]
    assert paths["/orders/{order_id}"]["get"]["operationId"] is not None

    # 4. POST /orders/{order_id}/cancel
    assert "/orders/{order_id}/cancel" in paths
    assert "post" in paths["/orders/{order_id}/cancel"]
    assert paths["/orders/{order_id}/cancel"]["post"]["operationId"] is not None

    # 5. POST /orders/{order_id}/checkout-session
    assert "/orders/{order_id}/checkout-session" in paths
    assert "post" in paths["/orders/{order_id}/checkout-session"]
    assert (
        paths["/orders/{order_id}/checkout-session"]["post"]["operationId"] is not None
    )

    # 6. GET /seller/shipments & POST /seller/shipments/{shipment_id}/ship
    assert "/seller/shipments" in paths
    assert "get" in paths["/seller/shipments"]
    assert "/seller/shipments/{shipment_id}" in paths
    assert "get" in paths["/seller/shipments/{shipment_id}"]
    assert "/seller/shipments/{shipment_id}/ship" in paths
    assert "post" in paths["/seller/shipments/{shipment_id}/ship"]

    # 7. Bearer сигурностна схема в компонентите
    security_schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "HTTPBearer" in security_schemes
    assert security_schemes["HTTPBearer"]["type"] == "http"
    assert security_schemes["HTTPBearer"]["scheme"] == "bearer"
