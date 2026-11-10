"""Интеграционни тестове за възстановяване на парола (POST /auth/password-reset/request и confirm)."""

import datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import get_account_mailer
from app.mailer import AccountMailer
from app.main import app
from app.models import PasswordResetToken
from app.service import _default_rate_limiter
from tests.conftest import create_user_and_token


def test_password_reset_full_lifecycle_and_single_use(
    client: TestClient, db_session: Session
) -> None:
    """Пълен цикъл на възстановяване: заявка, потвърждение, отхвърляне на стар токен и на повторен reset токен."""
    _default_rate_limiter.clear()
    email = "reset_user@example.com"
    old_pass = "OldSecurePassword123!"
    new_pass = "NewResetPassword2026!"

    _user, old_token = create_user_and_token(db_session, email=email, password=old_pass)

    # Захващаме токена чрез mock на мейлъра
    captured_tokens: list[str] = []
    mock_mailer = MagicMock(spec=AccountMailer)

    def capture_email(to_email: str, token: str, reset_url: str) -> None:
        captured_tokens.append(token)

    mock_mailer.send_password_reset_email.side_effect = capture_email
    app.dependency_overrides[get_account_mailer] = lambda: mock_mailer

    try:
        # 1. Заявка за възстановяване на паролата
        req_res = client.post(
            "/auth/password-reset/request",
            json={"email": email},
        )
        assert req_res.status_code == 202
        assert req_res.json() == {
            "message": "If the account exists and is eligible, password reset instructions have been sent."
        }
        assert len(captured_tokens) == 1
        plain_token = captured_tokens[0]

        # 2. Потвърждаване на новата парола с получения токен
        confirm_res = client.post(
            "/auth/password-reset/confirm",
            json={"token": plain_token, "new_password": new_pass},
        )
        assert confirm_res.status_code == 204

        # 3. Старият JWT токен се отхвърля (401 INVALID_TOKEN)
        old_jwt_res = client.get(
            "/users/me",
            headers={"Authorization": f"Bearer {old_token}"},
        )
        assert old_jwt_res.status_code == 401
        assert old_jwt_res.json()["detail"]["code"] == "INVALID_TOKEN"

        # 4. Старата парола вече не работи (401)
        failed_login = client.post(
            "/auth/login",
            json={"email": email, "password": old_pass},
        )
        assert failed_login.status_code == 401

        # 5. Новата парола работи успешно (200)
        new_login = client.post(
            "/auth/login",
            json={"email": email, "password": new_pass},
        )
        assert new_login.status_code == 200

        # 6. Повторно използване на същия reset токен се отхвърля (400 INVALID_RESET_TOKEN)
        re_confirm = client.post(
            "/auth/password-reset/confirm",
            json={"token": plain_token, "new_password": "AnotherNewPassword123!"},
        )
        assert re_confirm.status_code == 400
        assert re_confirm.json()["detail"]["code"] == "INVALID_RESET_TOKEN"
    finally:
        app.dependency_overrides.pop(get_account_mailer, None)


def test_password_reset_enumeration_protection(
    client: TestClient, db_session: Session
) -> None:
    """Заявка за несъществуващ имейл връща идентичен статус 202 и не записва токен."""
    _default_rate_limiter.clear()
    unknown_email = "non_existent_reset@example.com"

    res = client.post(
        "/auth/password-reset/request",
        json={"email": unknown_email},
    )
    assert res.status_code == 202
    assert res.json() == {
        "message": "If the account exists and is eligible, password reset instructions have been sent."
    }

    # Проверка, че в базата няма създаден токен
    tokens = db_session.scalars(select(PasswordResetToken)).all()
    assert len(tokens) == 0


def test_password_reset_inactive_user_ignored(
    client: TestClient, db_session: Session
) -> None:
    """Заявка за блокиран потребител връща 202, но не създава токен в базата."""
    _default_rate_limiter.clear()
    email = "blocked_reset@example.com"
    _user, _token = create_user_and_token(db_session, email=email, status="blocked")

    res = client.post(
        "/auth/password-reset/request",
        json={"email": email},
    )
    assert res.status_code == 202
    tokens = db_session.scalars(select(PasswordResetToken)).all()
    assert len(tokens) == 0


def test_password_reset_expired_token_rejected(
    client: TestClient, db_session: Session
) -> None:
    """Изтекъл токен (expires_at в миналото) се отхвърля с 400 INVALID_RESET_TOKEN."""
    import hashlib

    user, _token = create_user_and_token(db_session, email="expired_tok@example.com")
    plain_token = "expired_dummy_plain_token_123"
    token_hash = hashlib.sha256(plain_token.encode("utf-8")).hexdigest()

    expired_record = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10),
    )
    db_session.add(expired_record)
    db_session.commit()

    confirm_res = client.post(
        "/auth/password-reset/confirm",
        json={"token": plain_token, "new_password": "NewValidPassword12345!"},
    )
    assert confirm_res.status_code == 400
    assert confirm_res.json()["detail"]["code"] == "INVALID_RESET_TOKEN"


def test_password_reset_rate_limiting(client: TestClient, db_session: Session) -> None:
    """Превишаване на лимита от 5 заявки връща 429 RATE_LIMIT_EXCEEDED."""
    _default_rate_limiter.clear()
    email = "ratelimit_reset@example.com"
    _user, _token = create_user_and_token(db_session, email=email)

    for _ in range(5):
        res = client.post("/auth/password-reset/request", json={"email": email})
        assert res.status_code == 202

    # 6-и опит се отхвърля с 429
    exceeded_res = client.post("/auth/password-reset/request", json={"email": email})
    assert exceeded_res.status_code == 429
    assert exceeded_res.json()["detail"]["code"] == "RATE_LIMIT_EXCEEDED"
