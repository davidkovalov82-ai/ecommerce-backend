"""Тестове за health endpoint на Users."""

from fastapi.testclient import TestClient


def test_health_check_returns_ok(client: TestClient) -> None:
    """Проверка, че /health връща 200 OK и потвърждава връзката с базата данни."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
    }
