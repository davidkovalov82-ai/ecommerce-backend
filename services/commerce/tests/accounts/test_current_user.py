from datetime import UTC, datetime, timedelta

import jwt
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token

client = TestClient(app)


def test_get_current_user_profile_successful() -> None:
    """1. Успешен GET /users/me с валиден Bearer токен."""
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
    """2. Липсващ Authorization хедър връща 401 с WWW-Authenticate."""
    res = client.get("/users/me")
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"


def test_get_current_user_corrupted_token() -> None:
    """3. Повреден (невалиден) JWT токен връща 401 с код INVALID_TOKEN."""
    res = client.get("/users/me", headers={"Authorization": "Bearer not-a-valid-jwt"})
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "INVALID_TOKEN"


def test_get_current_user_wrong_signature() -> None:
    """4. Токен с грешен подпис (различен таен ключ) връща 401 INVALID_TOKEN."""
    wrong_secret = "different_secret_key_at_least_32_bytes_long_12345!"
    forged_token = create_access_token(user_id=1, secret=wrong_secret)

    res = client.get("/users/me", headers={"Authorization": f"Bearer {forged_token}"})
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "INVALID_TOKEN"


def test_get_current_user_expired_token_without_sleep() -> None:
    """5. Изтекъл токен връща 401 INVALID_TOKEN (без използване на sleep)."""
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
    """6. Липсващи задължителни claims (sub, iat, exp) или невалиден sub формат връщат 401."""
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

    # Липсващ iat
    t_no_iat = jwt.encode(
        {"sub": "1", "exp": now + 900}, settings.jwt_secret, algorithm="HS256"
    )
    r_no_iat = client.get("/users/me", headers={"Authorization": f"Bearer {t_no_iat}"})
    assert r_no_iat.status_code == 401
    assert r_no_iat.json()["detail"]["code"] == "INVALID_TOKEN"

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
    """7. Валиден токен за изтрит/несъществуващ в БД потребител връща 401 USER_NOT_FOUND."""
    # Уверяваме се, че ID 99999 не съществува
    assert db_session.get(User, 99999) is None

    token = create_access_token(user_id=99999, secret=settings.jwt_secret)
    res = client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "USER_NOT_FOUND"


def test_token_with_unsupported_algorithm_rejected() -> None:
    """8. Токен, подписан с HS384, се отхвърля при разрешен само HS256."""
    now = int(datetime.now(UTC).timestamp())
    token_hs384 = jwt.encode(
        {"sub": "1", "iat": now, "exp": now + 900},
        settings.jwt_secret,
        algorithm="HS384",
    )
    res = client.get("/users/me", headers={"Authorization": f"Bearer {token_hs384}"})
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "INVALID_TOKEN"


def test_create_access_token_exact_duration_900_seconds() -> None:
    """9. exp - iat е точно 900 секунди (15 минути) при стандартната конфигурация."""
    token = create_access_token(user_id=42, secret=settings.jwt_secret)
    decoded = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        options={"verify_signature": True},
    )
    assert decoded["exp"] - decoded["iat"] == 900
