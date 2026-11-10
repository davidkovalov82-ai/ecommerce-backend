"""Интеграционни тестове за автентикация (вход) в Users."""

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User


def test_login_successful_after_registration(
    client: TestClient,
    test_rsa_keys: tuple[str, str],
) -> None:
    """Успешен вход след регистрация: връща 200 с Bearer JWT access-токен, подписан с RS256."""
    _, public_pem = test_rsa_keys
    email = "auth_success@example.com"
    password = "super_secret_password_12345"

    reg_res = client.post("/users", json={"email": email, "password": password})
    assert reg_res.status_code == 201
    user_id = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200

    data = login_res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    # Декодиране и проверка на токена с публичния RSA ключ
    token = data["access_token"]
    decoded = jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        audience="ecommerce",
        issuer="ecommerce-users",
    )
    assert decoded["sub"] == str(user_id)
    assert decoded["role"] == "customer"
    assert decoded["email"] == email
    assert decoded["token_version"] == 0
    assert "iat" in decoded
    assert "exp" in decoded


def test_login_email_normalization(client: TestClient) -> None:
    """Нормализация на имейла (strip и lowercase), без промяна на интервалите в паролата."""
    email = "normalize_user@example.com"
    password_with_spaces = "  Password With Spaces 12345  "

    reg_res = client.post(
        "/users", json={"email": email, "password": password_with_spaces}
    )
    assert reg_res.status_code == 201

    # Вход с главни букви и интервали в имейла
    login_res = client.post(
        "/auth/login",
        json={
            "email": "  NORMALIZE_USER@EXAMPLE.COM  ",
            "password": password_with_spaces,
        },
    )
    assert login_res.status_code == 200

    # Опит за вход със стрипната парола трябва да се отхвърли с 401
    failed_login = client.post(
        "/auth/login",
        json={
            "email": email,
            "password": password_with_spaces.strip(),
        },
    )
    assert failed_login.status_code == 401


def test_login_invalid_credentials_identical_response(client: TestClient) -> None:
    """Еднакъв отговор (401, INVALID_CREDENTIALS, WWW-Authenticate) при грешна парола и неизвестен email."""
    email = "existing@example.com"
    password = "correct_password_12345"

    reg_res = client.post("/users", json={"email": email, "password": password})
    assert reg_res.status_code == 201

    # Грешна парола за съществуващ потребител
    wrong_pass_res = client.post(
        "/auth/login",
        json={"email": email, "password": "wrong_password_attempt_123"},
    )
    assert wrong_pass_res.status_code == 401
    assert wrong_pass_res.headers.get("WWW-Authenticate") == "Bearer"
    assert wrong_pass_res.json() == {
        "detail": {
            "code": "INVALID_CREDENTIALS",
            "message": "Invalid email or password",
        }
    }

    # Несъществуващ потребител
    unknown_email_res = client.post(
        "/auth/login",
        json={
            "email": "non_existent@example.com",
            "password": "any_random_password_123",
        },
    )
    assert unknown_email_res.status_code == 401
    assert unknown_email_res.headers.get("WWW-Authenticate") == "Bearer"
    assert unknown_email_res.json() == {
        "detail": {
            "code": "INVALID_CREDENTIALS",
            "message": "Invalid email or password",
        }
    }


def test_login_blocked_user_rejected(client: TestClient, db_session: Session) -> None:
    """Блокиран потребител (status='blocked') получава 403 ACCOUNT_BLOCKED."""
    email = "blocked_user@example.com"
    password = "secure_password_12345"

    reg_res = client.post("/users", json={"email": email, "password": password})
    assert reg_res.status_code == 201
    user_id = reg_res.json()["id"]

    # Променяме статуса на блокиран
    user = db_session.scalar(select(User).where(User.id == user_id))
    assert user is not None
    user.status = "blocked"
    db_session.commit()

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 403
    assert login_res.json() == {
        "detail": {
            "code": "ACCOUNT_BLOCKED",
            "message": "Account is blocked",
        }
    }


def test_login_closed_user_rejected(client: TestClient, db_session: Session) -> None:
    """Закрит профил (status='closed') получава 403 ACCOUNT_CLOSED."""
    email = "closed_user@example.com"
    password = "secure_password_12345"

    reg_res = client.post("/users", json={"email": email, "password": password})
    assert reg_res.status_code == 201
    user_id = reg_res.json()["id"]

    user = db_session.scalar(select(User).where(User.id == user_id))
    assert user is not None
    user.status = "closed"
    db_session.commit()

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 403
    assert login_res.json() == {
        "detail": {
            "code": "ACCOUNT_CLOSED",
            "message": "Account is closed",
        }
    }


def test_public_key_endpoint(
    client: TestClient, test_rsa_keys: tuple[str, str]
) -> None:
    """Endpoint /auth/public-key предоставя публичния ключ и метаданните за валидация."""
    _, public_pem = test_rsa_keys
    response = client.get("/auth/public-key")
    assert response.status_code == 200
    data = response.json()
    assert data["public_key"] == public_pem.strip()
    assert data["algorithm"] == "RS256"
    assert data["issuer"] == "ecommerce-users"
    assert data["audience"] == "ecommerce"
