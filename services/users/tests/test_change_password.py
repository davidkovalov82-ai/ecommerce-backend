"""Интеграционни тестове за смяна на парола (POST /users/me/change-password)."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import create_user_and_token


def test_change_password_full_lifecycle_and_token_invalidation(
    client: TestClient, db_session: Session
) -> None:
    """Успешна смяна на парола: старият парол спира, новият работи, предишните токени се отхвърлят."""
    email = "changepwd_user@example.com"
    old_pass = "InitialSecurePassword123!"
    new_pass = "BrandNewSuperSecret456!"

    _user, old_token = create_user_and_token(db_session, email=email, password=old_pass)

    # 1. Смяна на паролата
    change_res = client.post(
        "/users/me/change-password",
        headers={"Authorization": f"Bearer {old_token}"},
        json={
            "current_password": old_pass,
            "new_password": new_pass,
        },
    )
    assert change_res.status_code == 204

    # 2. Старият access-токен незабавно се отхвърля (401 INVALID_TOKEN)
    old_jwt_res = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert old_jwt_res.status_code == 401
    assert old_jwt_res.json()["detail"]["code"] == "INVALID_TOKEN"

    # 3. Вход със старата парола вече не работи (401)
    failed_login = client.post(
        "/auth/login",
        json={"email": email, "password": old_pass},
    )
    assert failed_login.status_code == 401

    # 4. Вход с новата парола работи успешно (200) и връща нов токен
    new_login = client.post(
        "/auth/login",
        json={"email": email, "password": new_pass},
    )
    assert new_login.status_code == 200
    new_token = new_login.json()["access_token"]

    # 5. Новият токен има пълен достъп
    new_me_res = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert new_me_res.status_code == 200
    assert new_me_res.json()["email"] == email


def test_change_password_invalid_current_password(
    client: TestClient, db_session: Session
) -> None:
    """Грешна текуща парола връща 400 INVALID_CURRENT_PASSWORD."""
    _user, token = create_user_and_token(
        db_session, email="wrong_curr_pwd@example.com", password="RealPassword12345!"
    )
    res = client.post(
        "/users/me/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": "WrongPasswordAttempt!",
            "new_password": "NewValidPassword12345!",
        },
    )
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "INVALID_CURRENT_PASSWORD"


def test_change_password_same_password_rejected(
    client: TestClient, db_session: Session
) -> None:
    """Опит за задаване на същата парола се отхвърля с 422."""
    _user, token = create_user_and_token(
        db_session, email="same_pwd@example.com", password="SamePassword12345!"
    )
    res = client.post(
        "/users/me/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": "SamePassword12345!",
            "new_password": "SamePassword12345!",
        },
    )
    assert res.status_code == 422


def test_change_password_too_short_new_password(
    client: TestClient, db_session: Session
) -> None:
    """Нова парола под 15 знака се отхвърля с 422."""
    _user, token = create_user_and_token(
        db_session, email="short_pwd@example.com", password="ValidPassword12345!"
    )
    res = client.post(
        "/users/me/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": "ValidPassword12345!",
            "new_password": "short_pass_123",
        },
    )
    assert res.status_code == 422
