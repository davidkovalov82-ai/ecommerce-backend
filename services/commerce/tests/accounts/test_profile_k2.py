from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token

client = TestClient(app)


def _create_user(
    db_session: Session,
    email: str,
    role: str = "customer",
    display_name: str | None = None,
) -> tuple[User, str]:
    user = User(
        email=email,
        password_hash="test_argon2_hash",
        role=role,
        display_name=display_name,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(user_id=user.id, secret=settings.jwt_secret)
    return user, token


def test_registration_without_name_and_legacy_profile(db_session: Session) -> None:
    """1. Регистрация без име продължава да работи, а стари профили четат display_name като null."""
    # Регистрация без име през API
    reg_res = client.post(
        "/users",
        json={
            "email": "fresh_user@example.com",
            "password": "ValidPassword12345!",
        },
    )
    assert reg_res.status_code == 201
    assert isinstance(reg_res.json()["id"], int)

    login_res = client.post(
        "/auth/login",
        json={
            "email": "fresh_user@example.com",
            "password": "ValidPassword12345!",
        },
    )
    fresh_token = login_res.json()["access_token"]

    # GET /users/me връща display_name=None
    me_res = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {fresh_token}"},
    )
    assert me_res.status_code == 200
    assert me_res.json()["display_name"] is None

    # Проверка на запис, създаден директно без display_name (като стар заварен потребител)
    legacy_user, legacy_token = _create_user(
        db_session, "legacy_user@example.com", display_name=None
    )
    legacy_me = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {legacy_token}"},
    )
    assert legacy_me.status_code == 200
    assert legacy_me.json() == {
        "id": legacy_user.id,
        "email": "legacy_user@example.com",
        "role": "customer",
        "display_name": None,
    }


def test_update_display_name_anna_and_isolation_with_boris_and_clear_with_null(
    db_session: Session,
) -> None:
    """2. Анна променя името си; профилът на Борис остава непокътнат; след това Анна го изчиства с null."""
    anna, anna_token = _create_user(db_session, "anna_profile@example.com")
    _boris, boris_token = _create_user(db_session, "boris_profile@example.com")

    # 1. Анна задава име с водещи/крайни интервали
    patch_anna = client.patch(
        "/users/me",
        json={"display_name": "  Анна  "},
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert patch_anna.status_code == 200
    assert patch_anna.json()["display_name"] == "Анна"

    # Нов GET /users/me за Анна потвърждава запазването
    get_anna = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert get_anna.status_code == 200
    assert get_anna.json()["display_name"] == "Анна"

    # Профилът на Борис остава непроменен
    get_boris = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {boris_token}"},
    )
    assert get_boris.status_code == 200
    assert get_boris.json()["display_name"] is None

    # Борис променя своето име независимо
    patch_boris = client.patch(
        "/users/me",
        json={"display_name": "Борис"},
        headers={"Authorization": f"Bearer {boris_token}"},
    )
    assert patch_boris.status_code == 200
    assert patch_boris.json()["display_name"] == "Борис"

    # Името на Анна си остава "Анна"
    get_anna_again = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert get_anna_again.json()["display_name"] == "Анна"

    # 2. Анна изчиства името си чрез явен null
    clear_anna = client.patch(
        "/users/me",
        json={"display_name": None},
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert clear_anna.status_code == 200
    assert clear_anna.json()["display_name"] is None

    # GET /users/me и директна проверка в БД потвърждават null
    get_anna_cleared = client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert get_anna_cleared.json()["display_name"] is None

    db_anna = db_session.scalar(select(User).where(User.id == anna.id))
    assert db_anna is not None
    assert db_anna.display_name is None


def test_contract_validation_errors(db_session: Session) -> None:
    """3. Проверка на валидационните грешки по договора и отхвърляне на неразрешени полета."""
    _user, token = _create_user(
        db_session, "validator_user@example.com", display_name="Оригинално Име"
    )
    auth_header = {"Authorization": f"Bearer {token}"}

    # Неавтентикирана заявка -> 401
    unauth = client.patch("/users/me", json={"display_name": "Ново Име"})
    assert unauth.status_code == 401
    assert unauth.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"

    # Заявка с допустимо име И поле role -> 422, без промяна на името или ролята
    r_role = client.patch(
        "/users/me",
        json={"display_name": "Име С Роля", "role": "admin"},
        headers=auth_header,
    )
    assert r_role.status_code == 422

    # Проверка, че името и ролята не са пипнати
    me = client.get("/users/me", headers=auth_header).json()
    assert me["display_name"] == "Оригинално Име"
    assert me["role"] == "customer"

    # Пуст обект {} -> 422
    r_empty_obj = client.patch("/users/me", json={}, headers=auth_header)
    assert r_empty_obj.status_code == 422

    # Празна стойност "" -> 422
    r_empty_str = client.patch(
        "/users/me", json={"display_name": ""}, headers=auth_header
    )
    assert r_empty_str.status_code == 422

    # Стойност само от интервали "   " -> 422
    r_spaces = client.patch(
        "/users/me", json={"display_name": "     "}, headers=auth_header
    )
    assert r_spaces.status_code == 422

    # Твърде дълго име (> 80 знака) -> 422
    r_too_long = client.patch(
        "/users/me", json={"display_name": "a" * 81}, headers=auth_header
    )
    assert r_too_long.status_code == 422

    # Валидно име с 80 знака -> 200
    r_exact_80 = client.patch(
        "/users/me", json={"display_name": "a" * 80}, headers=auth_header
    )
    assert r_exact_80.status_code == 200
    assert r_exact_80.json()["display_name"] == "a" * 80

    # Посторонни полета -> 422
    for forbidden_field, value in [
        ("id", 99),
        ("user_id", 99),
        ("email", "hacker@example.com"),
        ("password_hash", "malicious_hash"),
    ]:
        r_forbidden = client.patch(
            "/users/me",
            json={"display_name": "Име", forbidden_field: value},
            headers=auth_header,
        )
        assert r_forbidden.status_code == 422
