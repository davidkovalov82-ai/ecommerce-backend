from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.modules.accounts.service import _DUMMY_HASH

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


def test_password_and_hash_never_leaked_in_login_errors() -> None:
    """5. Паролата никога не изтича в отговорите или валидационните грешки (422) на /auth/login."""
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
