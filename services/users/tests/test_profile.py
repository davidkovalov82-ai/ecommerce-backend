"""Интеграционни тестове за преглед и редакция на потребителски профил (GET и PATCH /users/me)."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import create_user_and_token


def test_get_me_unauthenticated_and_authenticated(
    client: TestClient, db_session: Session
) -> None:
    """GET /users/me изисква валидна автентикация и връща данните на текущия потребител."""
    # 1. Без заглавка за автентикация -> 401
    unauth_res = client.get("/users/me")
    assert unauth_res.status_code == 401
    assert unauth_res.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"

    # 2. С валиден Bearer токен
    user, token = create_user_and_token(
        db_session,
        email="profile_me@example.com",
        display_name="Профил Тест",
    )
    auth_res = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert auth_res.status_code == 200
    assert auth_res.json() == {
        "id": user.id,
        "email": "profile_me@example.com",
        "role": "customer",
        "display_name": "Профил Тест",
    }


def test_patch_me_updates_display_name_and_clears_with_null(
    client: TestClient, db_session: Session
) -> None:
    """PATCH /users/me нормализира интервалите на display_name и позволява изчистване с null."""
    _user, token = create_user_and_token(
        db_session, email="anna_patch@example.com", display_name=None
    )
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Задаване на име с външни интервали
    r_name = client.patch(
        "/users/me",
        json={"display_name": "  Анна Иванова  "},
        headers=headers,
    )
    assert r_name.status_code == 200
    assert r_name.json()["display_name"] == "Анна Иванова"

    # 2. Изчистване с явен null
    r_clear = client.patch(
        "/users/me",
        json={"display_name": None},
        headers=headers,
    )
    assert r_clear.status_code == 200
    assert r_clear.json()["display_name"] is None


def test_patch_me_validation_and_tamper_protection(
    client: TestClient, db_session: Session
) -> None:
    """PATCH /users/me отхвърля невалидни имена, нулеви символи и опити за подмяна на роля или статус."""
    _user, token = create_user_and_token(
        db_session, email="tamper_test@example.com", display_name="Оригинално"
    )
    headers = {"Authorization": f"Bearer {token}"}

    # Опит за смяна на роля -> 422
    r_role = client.patch(
        "/users/me",
        json={"display_name": "Хакер", "role": "admin"},
        headers=headers,
    )
    assert r_role.status_code == 422

    # Опит за смяна на статус -> 422
    r_status = client.patch(
        "/users/me",
        json={"display_name": "Хакер", "status": "blocked"},
        headers=headers,
    )
    assert r_status.status_code == 422

    # Непозволени полета id, email, password_hash -> 422
    for forbidden in ["id", "email", "password_hash", "token_version"]:
        r_forbidden = client.patch(
            "/users/me",
            json={"display_name": "Име", forbidden: "any"},
            headers=headers,
        )
        assert r_forbidden.status_code == 422

    # Празен низ или само интервали -> 422
    assert (
        client.patch(
            "/users/me", json={"display_name": ""}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/users/me", json={"display_name": "   "}, headers=headers
        ).status_code
        == 422
    )

    # Дължина над 80 знака -> 422
    assert (
        client.patch(
            "/users/me", json={"display_name": "a" * 81}, headers=headers
        ).status_code
        == 422
    )

    # Валидно име с точно 80 знака -> 200
    exact_80 = client.patch(
        "/users/me", json={"display_name": "a" * 80}, headers=headers
    )
    assert exact_80.status_code == 200
    assert exact_80.json()["display_name"] == "a" * 80

    # Нулев символ \0 -> 422
    assert (
        client.patch(
            "/users/me", json={"display_name": "Тест\0"}, headers=headers
        ).status_code
        == 422
    )
