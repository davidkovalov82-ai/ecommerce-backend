"""Тестове за вътрешния API на Users (/internal/users/{user_id})."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User

INTERNAL_TOKEN = settings.internal_auth_token


def test_internal_get_user_missing_or_invalid_auth_token(client: TestClient) -> None:
    """Заявка без X-Internal-Token или с грешен токен се отхвърля с 401."""
    # Без хедър
    resp = client.get("/internal/users/1")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED_INTERNAL_REQUEST"

    # С грешен хедър
    resp2 = client.get(
        "/internal/users/1",
        headers={"X-Internal-Token": "wrong-secret-token"},
    )
    assert resp2.status_code == 401
    assert resp2.json()["detail"]["code"] == "UNAUTHORIZED_INTERNAL_REQUEST"


def test_internal_get_user_not_found(client: TestClient) -> None:
    """Заявка за несъществуващ потребител с валиден вътрешен токен връща 404."""
    resp = client.get(
        "/internal/users/99999",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "USER_NOT_FOUND"


def test_internal_get_user_success(client: TestClient, db_session: Session) -> None:
    """Успешно извличане на вътрешния статус, роля и token_version на потребител."""
    user = User(
        email="internal_test@example.com",
        password_hash="argon2id$mockhash",
        role="seller",
        status="active",
        display_name="Internal Merchant",
        token_version=2,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    resp = client.get(
        f"/internal/users/{user.id}",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == user.id
    assert data["email"] == "internal_test@example.com"
    assert data["role"] == "seller"
    assert data["status"] == "active"
    assert data["token_version"] == 2
    assert data["display_name"] == "Internal Merchant"

