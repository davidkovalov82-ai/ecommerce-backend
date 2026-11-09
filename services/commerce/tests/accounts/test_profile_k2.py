import concurrent.futures
from datetime import UTC, datetime

import jwt
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

    # Нулев символ (\0 / \x00) в display_name -> 422
    r_null_byte1 = client.patch(
        "/users/me", json={"display_name": "Анна\0"}, headers=auth_header
    )
    assert r_null_byte1.status_code == 422

    r_null_byte2 = client.patch(
        "/users/me", json={"display_name": "Ан\x00на"}, headers=auth_header
    )
    assert r_null_byte2.status_code == 422

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


def test_change_password_full_lifecycle_and_session_invalidation() -> None:
    """Сценарий: Анна издава 2 токена и сменя паролата. Старите токени се отзовават; Борис е незасегнат."""
    email_anna = "anna_pwd@example.com"
    pwd_old = "AnnaInitialSecret123!"
    pwd_new = "AnnaBrandNewPassword456!"

    # 1. Регистрация на Анна и издаване на два активни токена
    reg_anna = client.post("/users", json={"email": email_anna, "password": pwd_old})
    assert reg_anna.status_code == 201

    login1 = client.post("/auth/login", json={"email": email_anna, "password": pwd_old})
    assert login1.status_code == 200
    token1 = login1.json()["access_token"]

    login2 = client.post("/auth/login", json={"email": email_anna, "password": pwd_old})
    assert login2.status_code == 200
    token2 = login2.json()["access_token"]

    # 2. Регистрация на Борис и издаване на токен за Борис
    reg_boris = client.post(
        "/users",
        json={"email": "boris_pwd@example.com", "password": "BorisPassword123!"},
    )
    assert reg_boris.status_code == 201

    login_boris = client.post(
        "/auth/login",
        json={"email": "boris_pwd@example.com", "password": "BorisPassword123!"},
    )
    token_boris = login_boris.json()["access_token"]

    # И двата токена на Анна и токенът на Борис работят
    assert (
        client.get(
            "/users/me", headers={"Authorization": f"Bearer {token1}"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/users/me", headers={"Authorization": f"Bearer {token2}"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/users/me", headers={"Authorization": f"Bearer {token_boris}"}
        ).status_code
        == 200
    )

    # 3. Опит за смяна с грешна текуща парола -> 400
    err_res = client.post(
        "/users/me/change-password",
        json={"current_password": "WrongPassword999!", "new_password": pwd_new},
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert err_res.status_code == 400
    assert err_res.json()["detail"]["code"] == "INVALID_CURRENT_PASSWORD"

    # Грешката НЕ отзовава съществуващите сесии
    assert (
        client.get(
            "/users/me", headers={"Authorization": f"Bearer {token1}"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/users/me", headers={"Authorization": f"Bearer {token2}"}
        ).status_code
        == 200
    )

    # 4. Успешна смяна на паролата с token1 -> 204 без тяло
    success_res = client.post(
        "/users/me/change-password",
        json={"current_password": pwd_old, "new_password": pwd_new},
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert success_res.status_code == 204
    assert success_res.text == ""

    # 5. И двата стари токена на Анна губят достъп (401 INVALID_TOKEN)
    r_revoked1 = client.get("/users/me", headers={"Authorization": f"Bearer {token1}"})
    assert r_revoked1.status_code == 401
    assert r_revoked1.json()["detail"]["code"] == "INVALID_TOKEN"

    r_revoked2 = client.get("/users/me", headers={"Authorization": f"Bearer {token2}"})
    assert r_revoked2.status_code == 401
    assert r_revoked2.json()["detail"]["code"] == "INVALID_TOKEN"

    # 6. Вход със старата парола не работи (401)
    login_old = client.post(
        "/auth/login", json={"email": email_anna, "password": pwd_old}
    )
    assert login_old.status_code == 401
    assert login_old.json()["detail"]["code"] == "INVALID_CREDENTIALS"

    # Вход с новата парола работи и издава валиден нов токен
    login_new = client.post(
        "/auth/login", json={"email": email_anna, "password": pwd_new}
    )
    assert login_new.status_code == 200
    token_fresh = login_new.json()["access_token"]
    assert (
        client.get(
            "/users/me", headers={"Authorization": f"Bearer {token_fresh}"}
        ).status_code
        == 200
    )

    # 7. Токенът на Борис продължава да работи без прекъсване
    res_boris = client.get(
        "/users/me", headers={"Authorization": f"Bearer {token_boris}"}
    )
    assert res_boris.status_code == 200
    assert res_boris.json()["email"] == "boris_pwd@example.com"


def test_change_password_legacy_token_compatibility_without_version(
    db_session: Session,
) -> None:
    """Стар JWT без версия се третира като версия 0 и става невалиден след смяна на парола."""
    email = "legacy_jwt_user@example.com"
    pwd_old = "LegacyInitialPassword123!"
    pwd_new = "LegacyBrandNewPassword456!"

    user, _ = _create_user(db_session, email)
    # Създаваме токен без token_version claim (като от по-стара ревизия на системата)
    now = int(datetime.now(UTC).timestamp())
    legacy_payload = {
        "sub": str(user.id),
        "iat": now,
        "exp": now + 900,
    }
    legacy_token = jwt.encode(legacy_payload, settings.jwt_secret, algorithm="HS256")

    # Преди смяна на паролата токенът работи (версия 0 == версия 0)
    res_before = client.get(
        "/users/me", headers={"Authorization": f"Bearer {legacy_token}"}
    )
    assert res_before.status_code == 200

    # Сменяме паролата директно през API с този токен
    change_res = client.post(
        "/users/me/change-password",
        json={"current_password": "test_argon2_hash_invalid", "new_password": pwd_new},
        headers={"Authorization": f"Bearer {legacy_token}"},
    )
    # Понеже _create_user използва фалшив password_hash, задаваме реален хеш за теста
    from pwdlib import PasswordHash

    user.password_hash = PasswordHash.recommended().hash(pwd_old)
    db_session.commit()

    change_res = client.post(
        "/users/me/change-password",
        json={"current_password": pwd_old, "new_password": pwd_new},
        headers={"Authorization": f"Bearer {legacy_token}"},
    )
    assert change_res.status_code == 204

    # След смяна на паролата (версията в БД става 1), старият токен без версия връща 401
    res_after = client.get(
        "/users/me", headers={"Authorization": f"Bearer {legacy_token}"}
    )
    assert res_after.status_code == 401
    assert res_after.json()["detail"]["code"] == "INVALID_TOKEN"


def test_change_password_contract_validation_and_no_leakage(
    db_session: Session,
) -> None:
    """Проверка на валидацията на договора и гарантиране, че отговорът 422 не съдържа пароли."""
    email = "validator_pwd_user@example.com"
    pwd = "ValidCurrentPassword123!"
    user, token = _create_user(db_session, email)
    from pwdlib import PasswordHash

    user.password_hash = PasswordHash.recommended().hash(pwd)
    db_session.commit()

    auth_header = {"Authorization": f"Bearer {token}"}

    # 1. Липсваща автентикация -> 401
    unauth = client.post(
        "/users/me/change-password",
        json={"current_password": pwd, "new_password": "NewSecretPass12345!"},
    )
    assert unauth.status_code == 401

    # 2. Новата парола е твърде кратка (< 15 знака) -> 422
    secret_short = "Short_Pass1!"
    res_short = client.post(
        "/users/me/change-password",
        json={"current_password": pwd, "new_password": secret_short},
        headers=auth_header,
    )
    assert res_short.status_code == 422
    # Гарантираме, че паролата не се връща в отговора на грешката
    assert secret_short not in res_short.text
    assert pwd not in res_short.text

    # 3. Новата парола съвпада със старата -> 422
    res_same = client.post(
        "/users/me/change-password",
        json={"current_password": pwd, "new_password": pwd},
        headers=auth_header,
    )
    assert res_same.status_code == 422
    assert pwd not in res_same.text

    # 4. Посторонни полета -> 422
    res_extra = client.post(
        "/users/me/change-password",
        json={
            "current_password": pwd,
            "new_password": "ValidNewPassword12345!",
            "role": "admin",
        },
        headers=auth_header,
    )
    assert res_extra.status_code == 422

    # 5. Пробелите в новата парола НЕ се обрязват и се запазват точно
    pwd_with_spaces = "  PasswordWithOuterSpaces123!  "
    res_spaces = client.post(
        "/users/me/change-password",
        json={"current_password": pwd, "new_password": pwd_with_spaces},
        headers=auth_header,
    )
    assert res_spaces.status_code == 204

    # Вход с новата парола със запазени интервали работи
    login_spaces = client.post(
        "/auth/login", json={"email": email, "password": pwd_with_spaces}
    )
    assert login_spaces.status_code == 200

    # Вход с обрязани интервали НЕ работи
    login_trimmed = client.post(
        "/auth/login", json={"email": email, "password": pwd_with_spaces.strip()}
    )
    assert login_trimmed.status_code == 401


def test_concurrent_password_changes() -> None:
    """При две едновременни заявки с една стара парола успешно минава само една, другата връща 400."""
    email = "concur_pwd_user@example.com"
    old_pwd = "OriginalPasswordConcur123!"
    reg = client.post("/users", json={"email": email, "password": old_pwd})
    assert reg.status_code == 201

    login = client.post("/auth/login", json={"email": email, "password": old_pwd})
    token = login.json()["access_token"]

    def do_change(new_pwd: str):
        c = TestClient(app)
        return c.post(
            "/users/me/change-password",
            json={"current_password": old_pwd, "new_password": new_pwd},
            headers={"Authorization": f"Bearer {token}"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_change, "NewPasswordOneConcur123!")
        f2 = executor.submit(do_change, "NewPasswordTwoConcur123!")
        res1 = f1.result()
        res2 = f2.result()

    statuses = sorted([res1.status_code, res2.status_code])
    assert statuses == [204, 400]
