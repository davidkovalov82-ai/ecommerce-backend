"""Тест за персистентност на данните в PostgreSQL при рестартиране на услугата Users."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.dependencies import get_db_session
from app.main import app


def test_users_data_persists_across_service_restart(
    session_factory: sessionmaker,
) -> None:
    """Потребителите се запазват в PostgreSQL и са достъпни след рестартиране на приложението."""
    email = "persistent_user@example.com"
    password = "secure_password_persists_12345"

    # 1. Стартиране на инстанция 1 и регистрация
    with session_factory() as session1:
        app.dependency_overrides[get_db_session] = lambda: session1
        with TestClient(app) as client1:
            reg_res = client1.post(
                "/users",
                json={"email": email, "password": password},
            )
            assert reg_res.status_code == 201
            assert isinstance(reg_res.json()["id"], int)
        app.dependency_overrides.clear()

    # 2. Имитация на рестартиране на Users: напълно нова сесия и нов TestClient
    with session_factory() as session2:
        app.dependency_overrides[get_db_session] = lambda: session2
        with TestClient(app) as client2:
            # Потребителят може успешно да влезе след рестарта
            login_res = client2.post(
                "/auth/login",
                json={"email": email, "password": password},
            )
            assert login_res.status_code == 200
            assert "access_token" in login_res.json()

            # Опит за повторна регистрация на същия имейл след рестарта се отхвърля с 409
            dup_res = client2.post(
                "/users",
                json={"email": email, "password": password},
            )
            assert dup_res.status_code == 409
            assert dup_res.json()["detail"]["code"] == "EMAIL_ALREADY_EXISTS"
        app.dependency_overrides.clear()
