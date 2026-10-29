import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.repository import UserRepository
from app.modules.accounts.service import AccountService, is_unique_email_violation

client = TestClient(app)


def test_successful_user_registration(db_session: Session) -> None:
    """1. Успешна регистрация: връща 201, задава роля customer и записва нормализиран email."""
    response = client.post(
        "/users",
        json={
            "email": "  Customer.One@Example.com  ",
            "password": "valid_secure_password_123",
        },
    )
    assert response.status_code == 201
    data = response.json()

    assert isinstance(data["id"], int)
    assert data["email"] == "customer.one@example.com"
    assert data["role"] == "customer"
    assert "password" not in data
    assert "password_hash" not in data

    # Проверка на записа директно в базата данни
    user_in_db = db_session.scalar(
        select(User).where(User.email == "customer.one@example.com")
    )
    assert user_in_db is not None
    assert user_in_db.id == data["id"]
    assert user_in_db.role == "customer"


def test_duplicate_email_rejected_including_case_insensitive() -> None:
    """2. Повторен имейл се отхвърля с 409 EMAIL_ALREADY_EXISTS, включително при различен регистър."""
    # Първа регистрация
    res1 = client.post(
        "/users",
        json={
            "email": "bob@example.com",
            "password": "valid_secure_password_123",
        },
    )
    assert res1.status_code == 201

    # Втора регистрация с абсолютно същия имейл
    res2 = client.post(
        "/users",
        json={
            "email": "bob@example.com",
            "password": "valid_secure_password_123",
        },
    )
    assert res2.status_code == 409
    assert res2.json() == {
        "detail": {
            "code": "EMAIL_ALREADY_EXISTS",
            "message": "User with this email already exists",
        }
    }

    # Трета регистрация с различен регистър и външни интервали
    res3 = client.post(
        "/users",
        json={
            "email": "   BoB@EXAMPLE.COM   ",
            "password": "valid_secure_password_123",
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
def test_invalid_email_rejected(invalid_email: str) -> None:
    """3а. Невалиден имейл адрес връща 422 Unprocessable Entity."""
    response = client.post(
        "/users",
        json={
            "email": invalid_email,
            "password": "valid_secure_password_123",
        },
    )
    assert response.status_code == 422


def test_password_boundary_validation() -> None:
    """3б. Граници на дължината на паролата: [15, 128] символа."""
    # 14 символа (твърде кратка) -> 422
    res_too_short = client.post(
        "/users",
        json={
            "email": "user.short@example.com",
            "password": "p" * 14,
        },
    )
    assert res_too_short.status_code == 422

    # 15 символа (долна граница, валидна) -> 201
    res_min = client.post(
        "/users",
        json={
            "email": "user.min@example.com",
            "password": "p" * 15,
        },
    )
    assert res_min.status_code == 201

    # 128 символа (горна граница, валидна) -> 201
    res_max = client.post(
        "/users",
        json={
            "email": "user.max@example.com",
            "password": "p" * 128,
        },
    )
    assert res_max.status_code == 201

    # 129 символа (твърде дълга) -> 422
    res_too_long = client.post(
        "/users",
        json={
            "email": "user.toolong@example.com",
            "password": "p" * 129,
        },
    )
    assert res_too_long.status_code == 422


def test_cannot_self_assign_role_admin() -> None:
    """4. Невъзможност за самоназначаване като admin (допълнителни полета се отхвърлят)."""
    response = client.post(
        "/users",
        json={
            "email": "hacker@example.com",
            "password": "valid_secure_password_123",
            "role": "admin",
        },
    )
    assert response.status_code == 422

    # Потребителят не трябва да е създаден
    with client:
        check = client.post(
            "/users",
            json={
                "email": "hacker@example.com",
                "password": "valid_secure_password_123",
            },
        )
        assert check.status_code == 201
        assert check.json()["role"] == "customer"


def test_password_hash_stored_and_verifies_original(db_session: Session) -> None:
    """5. Записан хеш с Argon2, който проверява успешно оригиналната парола."""
    raw_password = "my_very_secret_pass_123"
    response = client.post(
        "/users",
        json={
            "email": "argon.test@example.com",
            "password": raw_password,
        },
    )
    assert response.status_code == 201
    user_id = response.json()["id"]

    user_in_db = db_session.get(User, user_id)
    assert user_in_db is not None
    # Хешът не бива да е чист текст
    assert user_in_db.password_hash != raw_password
    # Трябва да е валиден Argon2 хеш
    assert user_in_db.password_hash.startswith("$argon2id$")

    user_repo = UserRepository(db_session)
    account_service = AccountService(
        user_repository=user_repo,
        session=db_session,
        password_hasher=PasswordHash.recommended(),
    )
    # Проверка на оригиналната парола срещу хеша
    assert (
        account_service.verify_password(raw_password, user_in_db.password_hash) is True
    )
    # Проверка на грешна парола
    assert (
        account_service.verify_password(
            "wrong_password_attempt", user_in_db.password_hash
        )
        is False
    )


def test_password_and_hash_never_leaked_in_responses() -> None:
    """6. Липса на парола и хеш в HTTP отговорите (при успех и при валидационни грешки)."""
    secret_pass = "super_secret_test_password_123"

    # При валидационна грешка (напр. парола под минимума)
    short_secret = "secret_pass_14"
    res_err = client.post(
        "/users",
        json={
            "email": "leak.test@example.com",
            "password": short_secret,
        },
    )
    assert res_err.status_code == 422
    # Паролата не бива да присъства в тялото на отговора за грешка
    assert short_secret not in res_err.text
    assert "password_hash" not in res_err.text

    # При успешен отговор
    res_ok = client.post(
        "/users",
        json={
            "email": "leak.test@example.com",
            "password": secret_pass,
        },
    )
    assert res_ok.status_code == 201
    assert secret_pass not in res_ok.text
    assert "password_hash" not in res_ok.text
    assert "password" not in res_ok.json()


def test_unique_email_constraint_handling_at_database_level(
    db_session: Session,
) -> None:
    """Проверка, че само конкретното нарушение на uq_users_email се преобразува в зает имейл."""
    # Създаваме първи запис
    user1 = User(
        email="direct@example.com",
        password_hash="fakehash",
        role="customer",
    )
    db_session.add(user1)
    db_session.commit()

    # Опит за дублиран запис директно в сесията, за да тестваме IntegrityError
    user2 = User(
        email="direct@example.com",
        password_hash="fakehash2",
        role="customer",
    )
    db_session.add(user2)
    with pytest.raises(IntegrityError) as exc_info:
        db_session.commit()

    assert is_unique_email_violation(exc_info.value) is True
