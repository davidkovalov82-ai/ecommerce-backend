from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_payments_openapi_operations_and_boundary() -> None:
    """Проверява маршрутите на Payments и липсата на Commerce маршрути в OpenAPI."""
    response = client.get("/openapi.json")
    assert response.status_code == 200

    paths = response.json().get("paths", {})
    assert "/health" in paths
    assert "get" in paths["/health"]

    assert "/webhooks/stripe" in paths
    assert "post" in paths["/webhooks/stripe"]

    assert "/internal/payments/checkout-sessions" in paths
    assert "post" in paths["/internal/payments/checkout-sessions"]

    assert "/internal/payments/orders/{order_id}/cancel" in paths
    assert "post" in paths["/internal/payments/orders/{order_id}/cancel"]

    assert "/orders" not in paths
    assert "/products" not in paths
    assert "/cart" not in paths
