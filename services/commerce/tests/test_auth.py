from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.service import _DUMMY_HASH
from app.modules.accounts.tokens import create_access_token

client = TestClient(app)


def test_login_successful_after_registration() -> None:
    """1. Успешен вход след регистрация и връщане на Bearer токен."""
    email = "auth_success@example.com"
    password = "SuperSecretPassphrase123!"

    reg_res = client.post("/users", json={"email": email, "password": password})
    assert reg_res.status_code == 201

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200

    data = login_res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    # Проверка на токена
    token = data["access_token"]
    decoded = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        options={"verify_signature": True},
    )
    assert decoded["sub"] == str(reg_res.json()["id"])
    assert "iat" in decoded
    assert "exp" in decoded


def test_login_email_normalization() -> None:
    """2. Нормализация на имейла (strip и lowercase), без промяна на интервалите в паролата."""
    email = "normalize_login@example.com"
    password_with_spaces = "  PasswordWithExactSpaces123!  "

    reg_res = client.post(
        "/users", json={"email": email, "password": password_with_spaces}
    )
    assert reg_res.status_code == 201

    # Вход с главни букви и интервали в имейла
    login_res = client.post(
        "/auth/login",
        json={
            "email": "  NORMALIZE_LOGIN@EXAMPLE.COM  ",
            "password": password_with_spaces,
        },
    )
    assert login_res.status_code == 200

    # Опит за вход със стрипната парола трябва да се провали (интервалите в паролата се пазят)
    failed_login = client.post(
        "/auth/login",
        json={
            "email": email,
            "password": password_with_spaces.strip(),
        },
    )
    assert failed_login.status_code == 401


def test_login_invalid_credentials_identical_response() -> None:
    """3. Еднакъв отговор (401, INVALID_CREDENTIALS, WWW-Authenticate) при грешна парола и неизвестен email."""
    email = "existing_user@example.com"
    password = "CorrectSecretPassword123!"

    reg_res = client.post("/users", json={"email": email, "password": password})
    assert reg_res.status_code == 201

    # Грешна парола за съществуващ потребител
    wrong_pass_res = client.post(
        "/auth/login",
        json={"email": email, "password": "WrongPasswordAttempt123!"},
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
            "email": "non_existent_user@example.com",
            "password": "AnyRandomPassword123!",
        },
    )
    assert unknown_email_res.status_code == 401
    assert unknown_email_res.headers.get("WWW-Authenticate") == "Bearer"
    assert unknown_email_res.json() == wrong_pass_res.json()


def test_login_timing_mitigation_calls_dummy_hash() -> None:
    """4. Защита от timing attacks: при несъществуващ потребител се верифицира срещу dummy hash."""
    from app.modules.accounts.dependencies import _password_hasher

    with patch.object(
        _password_hasher, "verify", wraps=_password_hasher.verify
    ) as spy_verify:
        res = client.post(
            "/auth/login",
            json={
                "email": "definitely_unknown_user@example.com",
                "password": "SomeRandomPassword123!",
            },
        )
        assert res.status_code == 401
        # Проверяваме, че verify е извикан точно с предварително генерирания dummy hash
        assert spy_verify.call_count == 1
        called_args = spy_verify.call_args[0]
        assert called_args[0] == "SomeRandomPassword123!"
        assert called_args[1] == _DUMMY_HASH


def test_get_current_user_profile_successful() -> None:
    """5. Успешен GET /users/me с валиден Bearer токен."""
    email = "current_user_test@example.com"
    password = "ValidUserPassword123!"

    reg_res = client.post("/users", json={"email": email, "password": password})
    assert reg_res.status_code == 201
    user_id = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    token = login_res.json()["access_token"]

    me_res = client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 200
    assert me_res.json() == {
        "id": user_id,
        "email": email,
        "role": "customer",
    }


def test_get_current_user_missing_token() -> None:
    """6. Липсващ Authorization хедър връща 401 с WWW-Authenticate."""
    res = client.get("/users/me")
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"


def test_get_current_user_corrupted_token() -> None:
    """7. Повреден (невалиден) JWT токен връща 401 с код INVALID_TOKEN."""
    res = client.get("/users/me", headers={"Authorization": "Bearer not-a-valid-jwt"})
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "INVALID_TOKEN"


def test_get_current_user_wrong_signature() -> None:
    """8. Токен с грешен подпис (различен таен ключ) връща 401 INVALID_TOKEN."""
    wrong_secret = "different_secret_key_at_least_32_bytes_long_12345!"
    forged_token = create_access_token(user_id=1, secret=wrong_secret)

    res = client.get("/users/me", headers={"Authorization": f"Bearer {forged_token}"})
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "INVALID_TOKEN"


def test_get_current_user_expired_token_without_sleep() -> None:
    """9. Изтекъл токен връща 401 INVALID_TOKEN (без използване на sleep)."""
    now = datetime.now(UTC)
    # Генерираме токен, чийто exp е бил преди 5 минути
    expired_payload = {
        "sub": "1",
        "iat": int((now - timedelta(minutes=20)).timestamp()),
        "exp": int((now - timedelta(minutes=5)).timestamp()),
    }
    expired_token = jwt.encode(expired_payload, settings.jwt_secret, algorithm="HS256")

    res = client.get("/users/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "INVALID_TOKEN"


def test_get_current_user_missing_required_claims_and_invalid_sub() -> None:
    """10. Липсващи задължителни claims (sub, iat, exp) или невалиден sub формат връщат 401."""
    now = int(datetime.now(UTC).timestamp())

    # Липсващ sub
    t_no_sub = jwt.encode(
        {"iat": now, "exp": now + 900}, settings.jwt_secret, algorithm="HS256"
    )
    r1 = client.get("/users/me", headers={"Authorization": f"Bearer {t_no_sub}"})
    assert r1.status_code == 401
    assert r1.json()["detail"]["code"] == "INVALID_TOKEN"

    # Липсващ exp
    t_no_exp = jwt.encode(
        {"sub": "1", "iat": now}, settings.jwt_secret, algorithm="HS256"
    )
    r2 = client.get("/users/me", headers={"Authorization": f"Bearer {t_no_exp}"})
    assert r2.status_code == 401
    assert r2.json()["detail"]["code"] == "INVALID_TOKEN"

    # Невалиден sub формат (буквен низ)
    t_bad_sub = jwt.encode(
        {"sub": "not-an-int", "iat": now, "exp": now + 900},
        settings.jwt_secret,
        algorithm="HS256",
    )
    r3 = client.get("/users/me", headers={"Authorization": f"Bearer {t_bad_sub}"})
    assert r3.status_code == 401
    assert r3.json()["detail"]["code"] == "INVALID_TOKEN"

    # Отрицателен sub
    t_neg_sub = jwt.encode(
        {"sub": "-5", "iat": now, "exp": now + 900},
        settings.jwt_secret,
        algorithm="HS256",
    )
    r4 = client.get("/users/me", headers={"Authorization": f"Bearer {t_neg_sub}"})
    assert r4.status_code == 401
    assert r4.json()["detail"]["code"] == "INVALID_TOKEN"


def test_get_current_user_nonexistent_user(db_session: Session) -> None:
    """11. Валиден токен за изтрит/несъществуващ в БД потребител връща 401 USER_NOT_FOUND."""
    # Уверяваме се, че ID 99999 не съществува
    assert db_session.get(User, 99999) is None

    token = create_access_token(user_id=99999, secret=settings.jwt_secret)
    res = client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "USER_NOT_FOUND"


def test_password_and_hash_never_leaked_in_login_errors() -> None:
    """12. Паролата никога не изтича в отговорите или валидационните грешки (422) на /auth/login."""
    secret = "SuperSecretPassword123!"

    # Липсващ email при подадена парола
    res_no_email = client.post("/auth/login", json={"password": secret})
    assert res_no_email.status_code == 422
    assert secret not in res_no_email.text

    # Грешна структура на JSON (подаден суров стринг с парола)
    res_raw_str = client.post("/auth/login", json=secret)
    assert res_raw_str.status_code == 422
    assert secret not in res_raw_str.text

    # Невалиден JSON синтаксис
    res_malformed = client.post(
        "/auth/login",
        content=f'{{"password": "{secret}",',
        headers={"Content-Type": "application/json"},
    )
    assert res_malformed.status_code == 422
    assert secret not in res_malformed.text
