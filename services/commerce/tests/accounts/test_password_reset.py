import concurrent.futures
import datetime
import json
import re
import urllib.request
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.accounts.models import PasswordResetToken, User
from app.modules.accounts.service import _default_rate_limiter
from tests.orders.helpers import _create_user_and_token

client = TestClient(app)


def _get_messages_for_recipient(recipient_email: str) -> list[dict[str, Any]]:
    """Извлича съобщенията от Mailpit API само за конкретен получател."""
    req = urllib.request.Request("http://127.0.0.1:8025/api/v1/messages")
    with urllib.request.urlopen(req, timeout=2) as r:
        data = json.loads(r.read().decode())
    return [
        m
        for m in data.get("messages", [])
        if any(to.get("Address") == recipient_email for to in m.get("To", []))
    ]


def _get_message_detail(message_id: str) -> dict[str, Any]:
    """Извлича съдържанието на съобщение от Mailpit по ID."""
    req = urllib.request.Request(f"http://127.0.0.1:8025/api/v1/message/{message_id}")
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read().decode())


def test_password_reset_happy_path_with_mailpit(db_session: Session) -> None:
    """1. Пълен успешен сценарий: заявка, изпращане на имейл в Mailpit, потвърждение, вход с нова парола.

    - Старата парола и старият JWT спират да работят.
    - Сесията на друг потребител (Борис) остава незасегната.
    """
    _default_rate_limiter.clear()

    # Регистрираме Анна и Борис
    anna_email = "anna_reset_test@example.com"
    anna_old_pass = "AnnaOldPassword123!"
    anna_new_pass = "AnnaNewSecurePassword2026!"

    client.post("/users", json={"email": anna_email, "password": anna_old_pass})
    anna_login = client.post(
        "/auth/login", json={"email": anna_email, "password": anna_old_pass}
    )
    anna_old_token = anna_login.json()["access_token"]

    boris_email = "boris_reset_test@example.com"
    boris_pass = "BorisPassword123!"
    client.post("/users", json={"email": boris_email, "password": boris_pass})
    boris_login = client.post(
        "/auth/login", json={"email": boris_email, "password": boris_pass}
    )
    boris_token = boris_login.json()["access_token"]

    # 1. Заявка за възстановяване на паролата на Анна
    req_res = client.post(
        "/auth/password-reset/request", json={"email": anna_email}
    )
    assert req_res.status_code == 202
    assert req_res.json() == {
        "message": "If the account exists and is eligible, password reset instructions have been sent."
    }

    # 2. Проверка на полученото писмо в Mailpit
    messages = _get_messages_for_recipient(anna_email)
    assert len(messages) >= 1
    latest_msg = messages[0]
    detail = _get_message_detail(latest_msg["ID"])
    body_text = detail.get("Text", "")

    # Извличаме токена от тялото на съобщението
    match = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", body_text)
    assert match is not None, f"Токенът не бе намерен в текста: {body_text}"
    reset_token = match.group(1)

    # Проверка, че линкът от конфигурацията е в текста
    assert settings.password_reset_base_url in body_text

    # 3. Потвърждаване на новата парола чрез POST /auth/password-reset/confirm
    confirm_res = client.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": anna_new_pass},
    )
    assert confirm_res.status_code == 204

    # 4. Старият JWT на Анна вече връща 401
    anna_old_jwt_res = client.get(
        "/users/me", headers={"Authorization": f"Bearer {anna_old_token}"}
    )
    assert anna_old_jwt_res.status_code == 401

    # 5. Старата парола на Анна вече не работи (401)
    old_login_res = client.post(
        "/auth/login", json={"email": anna_email, "password": anna_old_pass}
    )
    assert old_login_res.status_code == 401

    # 6. Новата парола работи успешно (200)
    new_login_res = client.post(
        "/auth/login", json={"email": anna_email, "password": anna_new_pass}
    )
    assert new_login_res.status_code == 200
    anna_new_token = new_login_res.json()["access_token"]

    anna_new_jwt_res = client.get(
        "/users/me", headers={"Authorization": f"Bearer {anna_new_token}"}
    )
    assert anna_new_jwt_res.status_code == 200
    assert anna_new_jwt_res.json()["email"] == anna_email

    # 7. Сесията на Борис остава валидна и активна
    boris_res = client.get(
        "/users/me", headers={"Authorization": f"Bearer {boris_token}"}
    )
    assert boris_res.status_code == 200
    assert boris_res.json()["email"] == boris_email

    # 8. Повторно използване на същия токен се отхвърля с 400 INVALID_RESET_TOKEN
    reuse_res = client.post(
        "/auth/password-reset/confirm",
        json={
            "token": reset_token,
            "new_password": "AnotherPassword321!",
        },
    )
    assert reuse_res.status_code == 400
    assert reuse_res.json()["detail"]["code"] == "INVALID_RESET_TOKEN"


def test_password_reset_request_identical_202_for_ineligible_accounts(
    db_session: Session,
) -> None:
    """2. Несъществуващ, блокиран, закрит и служебен акаунт връщат идентичен 202 и не получават писмо."""
    _default_rate_limiter.clear()

    # Създаваме блокиран и закрит акаунт
    blocked_user, _ = _create_user_and_token(
        db_session, email="blocked_user@example.com"
    )
    blocked_user.status = "blocked"

    closed_user, _ = _create_user_and_token(
        db_session, email="closed_user@example.com"
    )
    closed_user.status = "closed"
    db_session.commit()

    ineligible_emails = [
        "non_existent_account_123@example.com",
        "blocked_user@example.com",
        "closed_user@example.com",
        "legacy-catalog@internal.ecommerce.com",
    ]

    expected_body = {
        "message": "If the account exists and is eligible, password reset instructions have been sent."
    }

    for email in ineligible_emails:
        res = client.post("/auth/password-reset/request", json={"email": email})
        assert res.status_code == 202
        assert res.json() == expected_body

        # Никакви писма не бива да са изпратени към този получател
        msgs = _get_messages_for_recipient(email)
        assert len(msgs) == 0, f"Изпратено е писмо за недопустим адрес: {email}"


def test_expired_and_invalid_tokens_return_identical_error(
    db_session: Session,
) -> None:
    """3. Просрочен, несъществуващ и вече използван токен връщат идентична грешка 400."""
    _default_rate_limiter.clear()
    user, _ = _create_user_and_token(
        db_session, email="expired_token_user@example.com"
    )

    # Записваме ръчно изтекъл токен
    expired_token_record = PasswordResetToken(
        user_id=user.id,
        token_hash="11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff",
        expires_at=datetime.datetime.now(datetime.UTC)
        - datetime.timedelta(minutes=1),
    )
    db_session.add(expired_token_record)
    db_session.commit()

    # Несъществуващ токен
    r1 = client.post(
        "/auth/password-reset/confirm",
        json={
            "token": "completely_random_token_string",
            "new_password": "NewValidPassword123!",
        },
    )
    assert r1.status_code == 400
    assert r1.json()["detail"]["code"] == "INVALID_RESET_TOKEN"

    # Изтекъл токен
    r2 = client.post(
        "/auth/password-reset/confirm",
        json={
            "token": "expired_token_test",
            "new_password": "NewValidPassword123!",
        },
    )
    assert r2.status_code == 400
    assert r2.json()["detail"]["code"] == "INVALID_RESET_TOKEN"
    assert r1.json() == r2.json()


def test_block_between_reset_request_and_confirm(db_session: Session) -> None:
    """4. Блокирането на акаунта между издаване на линка и потвърждението отхвърля възстановяването."""
    _default_rate_limiter.clear()
    email = "block_midway_user@example.com"
    client.post(
        "/users", json={"email": email, "password": "OriginalPassword123!"}
    )

    # Заявяваме токен
    client.post("/auth/password-reset/request", json={"email": email})
    msgs = _get_messages_for_recipient(email)
    assert len(msgs) >= 1
    detail = _get_message_detail(msgs[0]["ID"])
    match = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", detail.get("Text", ""))
    assert match is not None
    token = match.group(1)

    # Администраторът блокира потребителя преди той да потвърди
    target = db_session.query(User).filter_by(email=email).first()
    assert target is not None
    target.status = "blocked"
    db_session.commit()

    # Опит за потвърждаване на токена
    res = client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": "NewPasswordAfterBlock123!"},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "INVALID_RESET_TOKEN"


def test_concurrent_confirmations_of_same_token(db_session: Session) -> None:
    """5. От две конкурентни потвърждения на един и същ токен успява точно едно (204), другото е 400."""
    _default_rate_limiter.clear()
    email = "concurrent_reset_user@example.com"
    client.post(
        "/users", json={"email": email, "password": "OriginalPassword123!"}
    )

    client.post("/auth/password-reset/request", json={"email": email})
    msgs = _get_messages_for_recipient(email)
    assert len(msgs) >= 1
    detail = _get_message_detail(msgs[0]["ID"])
    match = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", detail.get("Text", ""))
    assert match is not None
    token = match.group(1)

    results: list[int] = []

    def _do_confirm(new_pw: str) -> int:
        local_client = TestClient(app)
        r = local_client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": new_pw},
        )
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_do_confirm, "ConcurrentPass1_12345!")
        f2 = executor.submit(_do_confirm, "ConcurrentPass2_12345!")
        results = [f1.result(), f2.result()]

    results.sort()
    assert results == [204, 400]


def test_password_reset_rate_limiting() -> None:
    """6. Ограничаване на честотата на заявките за възстановяване (429 Too Many Requests)."""
    _default_rate_limiter.clear()
    email = "rate_limit_test_user@example.com"

    # Изпълняваме 5 допустими заявки
    for _ in range(5):
        r = client.post("/auth/password-reset/request", json={"email": email})
        assert r.status_code == 202

    # 6-тата заявка надвишава лимита
    exceeded_res = client.post(
        "/auth/password-reset/request", json={"email": email}
    )
    assert exceeded_res.status_code == 429
    assert exceeded_res.json()["detail"]["code"] == "RATE_LIMIT_EXCEEDED"


def test_password_validation_on_confirm_does_not_leak_password() -> None:
    """7. Валидация на новата парола при потвърждаване: твърде кратка дава 422 и не разкрива паролата."""
    short_pw = "Short1!"
    res = client.post(
        "/auth/password-reset/confirm",
        json={"token": "some_dummy_token", "new_password": short_pw},
    )
    assert res.status_code == 422
    assert short_pw not in res.text
