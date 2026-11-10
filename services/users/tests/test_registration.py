"""Интеграционни тестове за регистрация на потребители в Users."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User


def test_successful_registration(client: TestClient, db_session: Session) -> None:
    """Успешна регистрация: връща 201, задава роля customer и нормализира имейла."""
    response = client.post(
        "/users",
        json={
            "email": "  Alice.New@Example.COM  ",
            "password": "secure_super_password_12345",
        },
    )
    assert response.status_code == 201
    data = response.json()

    assert isinstance(data["id"], int)
    assert data["email"] == "alice.new@example.com"
    assert data["role"] == "customer"
    assert data["display_name"] is None
    assert "password" not in data
    assert "password_hash" not in data

    # Проверка на записа директно в PostgreSQL
    user_in_db = db_session.scalar(
        select(User).where(User.email == "alice.new@example.com")
    )
    assert user_in_db is not None
    assert user_in_db.id == data["id"]
    assert user_in_db.role == "customer"
    assert user_in_db.status == "active"
    assert user_in_db.token_version == 0
    assert user_in_db.password_hash.startswith("$argon2")


def test_duplicate_email_rejected(client: TestClient) -> None:
    """Повторен имейл се отхвърля с 409 EMAIL_ALREADY_EXISTS, включително при регистър и интервали."""
    # Първа регистрация
    res1 = client.post(
        "/users",
        json={
            "email": "bob@example.com",
            "password": "secure_super_password_12345",
        },
    )
    assert res1.status_code == 201

    # Втора регистрация с абсолютно същия имейл
    res2 = client.post(
        "/users",
        json={
            "email": "bob@example.com",
            "password": "secure_super_password_12345",
        },
    )
    assert res2.status_code == 409
    assert res2.json() == {
        "detail": {
            "code": "EMAIL_ALREADY_EXISTS",
            "message": "User with this email already exists",
        }
    }

    # Трета регистрация с главни букви и интервали
    res3 = client.post(
        "/users",
        json={
            "email": "   BOB@EXAMPLE.COM   ",
            "password": "secure_super_password_12345",
        },
    )
    assert res3.status_code == 409
    assert res3.json()["detail"]["code"] == "EMAIL_ALREADY_EXISTS"


@pytest.mark.parametrize(
    "invalid_email",
    [
        "plainaddress",
        "@missingusername.com",
        "username@.com",
        "username@com",
        "",
        "   ",
    ],
)
def test_invalid_email_rejected(client: TestClient, invalid_email: str) -> None:
    """Невалиден имейл адрес връща 422 Unprocessable Entity."""
    response = client.post(
        "/users",
        json={
            "email": invalid_email,
            "password": "secure_super_password_12345",
        },
    )
    assert response.status_code == 422


def test_password_length_validation(client: TestClient) -> None:
    """Парола под 15 или над 128 знака се отхвърля с 422."""
    # Под 15 знака
    too_short = client.post(
        "/users",
        json={
            "email": "valid1@example.com",
            "password": "short_pass_123",  # 14 знака
        },
    )
    assert too_short.status_code == 422

    # Над 128 знака
    too_long = client.post(
        "/users",
        json={
            "email": "valid2@example.com",
            "password": "a" * 129,
        },
    )
    assert too_long.status_code == 422

    # Точно 15 знака
    exact_min = client.post(
        "/users",
        json={
            "email": "valid3@example.com",
            "password": "a" * 15,
        },
    )
    assert exact_min.status_code == 201


def test_client_cannot_assign_admin_rights(client: TestClient) -> None:
    """Клиентът не може да си присвои административна роля или други непозволени полета."""
    response = client.post(
        "/users",
        json={
            "email": "hacker@example.com",
            "password": "secure_super_password_12345",
            "role": "admin",
        },
    )
    assert response.status_code == 422
