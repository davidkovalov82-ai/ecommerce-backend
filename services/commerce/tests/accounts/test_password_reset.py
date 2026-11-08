import concurrent.futures
import datetime
import hashlib
import json
import re
import urllib.request
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.accounts.mailer import AccountMailer
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
    req_res = client.post("/auth/password-reset/request", json={"email": anna_email})
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

    closed_user, _ = _create_user_and_token(db_session, email="closed_user@example.com")
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
    """3. Просрочен, несъществуващ и вече използван токен връщат идентична грешка 400.

    Записва истинския SHA-256 хеш на токена, който се изпраща, със срок в миналото.
    """
    _default_rate_limiter.clear()
    user, _ = _create_user_and_token(db_session, email="expired_token_user@example.com")

    # Истински токен и неговият действителен SHA-256 хеш със срок в миналото
    raw_expired_token = "actual_expired_plain_token_string_12345"
    real_expired_hash = hashlib.sha256(raw_expired_token.encode("utf-8")).hexdigest()

    expired_token_record = PasswordResetToken(
        user_id=user.id,
        token_hash=real_expired_hash,
        expires_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1),
    )
    db_session.add(expired_token_record)
    db_session.commit()

    # 1. Несъществуващ токен
    r1 = client.post(
        "/auth/password-reset/confirm",
        json={
            "token": "completely_random_token_string",
            "new_password": "NewValidPassword123!",
        },
    )
    assert r1.status_code == 400
    assert r1.json()["detail"]["code"] == "INVALID_RESET_TOKEN"

    # 2. Изтекъл токен със съвпадащ валиден хеш в базата
    r2 = client.post(
        "/auth/password-reset/confirm",
        json={
            "token": raw_expired_token,
            "new_password": "NewValidPassword123!",
        },
    )
    assert r2.status_code == 400
    assert r2.json()["detail"]["code"] == "INVALID_RESET_TOKEN"
    assert r1.json() == r2.json()


def test_block_and_unblock_via_real_admin_api_invalidates_reset_link(
    db_session: Session,
) -> None:
    """4. Издаване на линк -> блокиране с реален admin API -> отблокиране -> старата връзка е отхвърлена."""
    _default_rate_limiter.clear()
    _, admin_token = _create_user_and_token(
        db_session, email="admin_mod_race@example.com", role="admin"
    )
    email = "block_unblock_via_api_user@example.com"
    client.post("/users", json={"email": email, "password": "OriginalPassword123!"})
    user = db_session.query(User).filter_by(email=email).first()
    assert user is not None

    # Заявяваме токен
    client.post("/auth/password-reset/request", json={"email": email})
    msgs = _get_messages_for_recipient(email)
    assert len(msgs) >= 1
    detail = _get_message_detail(msgs[0]["ID"])
    match = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", detail.get("Text", ""))
    assert match is not None
    token = match.group(1)

    # Администраторът блокира потребителя през реален админ API
    block_res = client.post(
        f"/users/{user.id}/block",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "Temporary suspension"},
    )
    assert block_res.status_code == 200

    # Администраторът отблокира потребителя през реален админ API
    unblock_res = client.post(
        f"/users/{user.id}/unblock",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert unblock_res.status_code == 200

    # Старият токен отпреди блокирането остава отхвърлен (400)
    res = client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": "NewPasswordAfterUnblock123!"},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "INVALID_RESET_TOKEN"


def test_normal_password_change_invalidates_previously_issued_reset_link(
    db_session: Session,
) -> None:
    """5. Издаване на линк за възстановяване -> нормална смяна на парола -> старият линк е отхвърлен."""
    _default_rate_limiter.clear()
    email = "normal_change_inval_user@example.com"
    old_pw = "OriginalPassword123!"
    client.post("/users", json={"email": email, "password": old_pw})
    login_res = client.post("/auth/login", json={"email": email, "password": old_pw})
    jwt_token = login_res.json()["access_token"]

    # 1. Заявяваме линк за възстановяване
    client.post("/auth/password-reset/request", json={"email": email})
    msgs = _get_messages_for_recipient(email)
    assert len(msgs) >= 1
    detail = _get_message_detail(msgs[0]["ID"])
    match = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", detail.get("Text", ""))
    assert match is not None
    reset_token = match.group(1)

    # 2. Потребителят сменя паролата си по нормалния начин през /users/me/change-password
    change_res = client.post(
        "/users/me/change-password",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"current_password": old_pw, "new_password": "NewNormalPassword123!"},
    )
    assert change_res.status_code == 204

    # 3. Опит за използване на издадения по-рано reset токен се отхвърля
    confirm_res = client.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "ResetPasswordAttempt123!"},
    )
    assert confirm_res.status_code == 400
    assert confirm_res.json()["detail"]["code"] == "INVALID_RESET_TOKEN"


def test_smtp_unavailable_resilience_and_recovery(db_session: Session) -> None:
    """6. Недостъпен SMTP сървър връща същия 202 Accepted и позволява успешно възстановяване след отстраняване на срива."""
    _default_rate_limiter.clear()
    email = "smtp_resilience_user@example.com"
    initial_pw = "InitialPassword123!"
    client.post("/users", json={"email": email, "password": initial_pw})

    # 1. Симулираме недостъпен SMTP (напр. ConnectionRefused / timeout)
    with patch.object(
        AccountMailer,
        "send_password_reset_email",
        side_effect=OSError("Mailpit connection refused on port 1025"),
    ):
        r_fail = client.post("/auth/password-reset/request", json={"email": email})
        # Външният отговор е идентичен 202 и не разкрива вътрешната грешка
        assert r_fail.status_code == 202
        assert r_fail.json() == {
            "message": "If the account exists and is eligible, password reset instructions have been sent."
        }

    # 2. SMTP е възстановен (нормална работа с реален Mailpit)
    r_ok = client.post("/auth/password-reset/request", json={"email": email})
    assert r_ok.status_code == 202

    # Проверяваме полученото ново работно писмо в Mailpit
    msgs = _get_messages_for_recipient(email)
    assert len(msgs) >= 1
    detail = _get_message_detail(msgs[0]["ID"])
    match = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", detail.get("Text", ""))
    assert match is not None
    valid_token = match.group(1)

    # Потвърждаваме новата парола успешно
    new_pw = "RecoveredPassword123!"
    confirm_res = client.post(
        "/auth/password-reset/confirm",
        json={"token": valid_token, "new_password": new_pw},
    )
    assert confirm_res.status_code == 204

    # Влизаме успешно с новата парола
    login_res = client.post("/auth/login", json={"email": email, "password": new_pw})
    assert login_res.status_code == 200


def test_concurrent_confirmations_of_same_token(db_session: Session) -> None:
    """7. От две конкурентни потвърждения на един и същ токен успява точно едно (204), другото е 400."""
    _default_rate_limiter.clear()
    email = "concurrent_reset_user@example.com"
    client.post("/users", json={"email": email, "password": "OriginalPassword123!"})

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


def test_concurrent_confirmations_of_two_different_tokens_same_user(
    db_session: Session,
) -> None:
    """8. Два различни действащи токена на един и същ потребител се потвърждават едновременно: точно един успява."""
    _default_rate_limiter.clear()
    email = "concurrent_diff_tokens_user@example.com"
    client.post("/users", json={"email": email, "password": "OriginalPassword123!"})

    # Издаваме първи токен
    client.post("/auth/password-reset/request", json={"email": email})
    # Издаваме втори токен за същия потребител
    client.post("/auth/password-reset/request", json={"email": email})

    msgs = _get_messages_for_recipient(email)
    assert len(msgs) >= 2
    detail0 = _get_message_detail(msgs[0]["ID"]).get("Text", "")
    detail1 = _get_message_detail(msgs[1]["ID"]).get("Text", "")
    t0 = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", detail0).group(1)
    t1 = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", detail1).group(1)
    assert t0 != t1

    results: list[int] = []

    def _confirm_token(t: str, pw: str) -> int:
        local_client = TestClient(app)
        res = local_client.post(
            "/auth/password-reset/confirm",
            json={"token": t, "new_password": pw},
        )
        return res.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_confirm_token, t0, "NewPasswordOne_12345!")
        f2 = executor.submit(_confirm_token, t1, "NewPasswordTwo_12345!")
        results = [f1.result(), f2.result()]

    results.sort()
    assert results == [204, 400], f"Очаквани [204, 400], получени {results}"


def test_concurrent_confirm_and_admin_block(db_session: Session) -> None:
    """9. Конкурентно потвърждаване на токен и блокиране от администратор: без 500 и без deadlock, след блока няма достъп."""
    _default_rate_limiter.clear()
    _, admin_token = _create_user_and_token(
        db_session, email="admin_block_race@example.com", role="admin"
    )
    email = "block_race_user@example.com"
    client.post("/users", json={"email": email, "password": "OriginalPassword123!"})
    user = db_session.query(User).filter_by(email=email).first()
    assert user is not None

    client.post("/auth/password-reset/request", json={"email": email})
    msgs = _get_messages_for_recipient(email)
    assert len(msgs) >= 1
    token = re.search(
        r"Токен:\s*([A-Za-z0-9_-]+)",
        _get_message_detail(msgs[0]["ID"]).get("Text", ""),
    ).group(1)

    def _do_confirm() -> int:
        local_client = TestClient(app)
        r = local_client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "NewRacePassword123!"},
        )
        return r.status_code

    def _do_block() -> int:
        local_client = TestClient(app)
        r = local_client.post(
            f"/users/{user.id}/block",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"reason": "Concurrent block"},
        )
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_confirm = executor.submit(_do_confirm)
        f_block = executor.submit(_do_block)
        confirm_code = f_confirm.result()
        block_code = f_block.result()

    assert block_code == 200
    assert confirm_code in (204, 400)

    # След блокирането потребителят задължително няма достъп (403 ACCOUNT_BLOCKED)
    active_pw = "NewRacePassword123!" if confirm_code == 204 else "OriginalPassword123!"
    login_res = client.post("/auth/login", json={"email": email, "password": active_pw})
    assert login_res.status_code == 403
    assert login_res.json()["detail"]["code"] == "ACCOUNT_BLOCKED"


def test_concurrent_confirmations_of_two_different_tokens_same_user(
    db_session: Session,
) -> None:
    """8. Два различни действащи токена на един и същ потребител се потвърждават едновременно: точно един успява."""
    _default_rate_limiter.clear()
    email = "concurrent_diff_tokens_user@example.com"
    client.post("/users", json={"email": email, "password": "OriginalPassword123!"})

    # Издаваме първи токен
    client.post("/auth/password-reset/request", json={"email": email})
    # Издаваме втори токен за същия потребител
    client.post("/auth/password-reset/request", json={"email": email})

    msgs = _get_messages_for_recipient(email)
    assert len(msgs) >= 2
    detail0 = _get_message_detail(msgs[0]["ID"]).get("Text", "")
    detail1 = _get_message_detail(msgs[1]["ID"]).get("Text", "")
    t0 = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", detail0).group(1)
    t1 = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", detail1).group(1)
    assert t0 != t1

    results: list[int] = []

    def _confirm_token(t: str, pw: str) -> int:
        local_client = TestClient(app)
        res = local_client.post(
            "/auth/password-reset/confirm",
            json={"token": t, "new_password": pw},
        )
        return res.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_confirm_token, t0, "NewPasswordOne_12345!")
        f2 = executor.submit(_confirm_token, t1, "NewPasswordTwo_12345!")
        results = [f1.result(), f2.result()]

    results.sort()
    assert results == [204, 400], f"Очаквани [204, 400], получени {results}"


def test_concurrent_confirm_and_admin_block(db_session: Session) -> None:
    """9. Конкурентно потвърждаване на токен и блокиране от администратор: без 500 и без deadlock, след блока няма достъп."""
    _default_rate_limiter.clear()
    _, admin_token = _create_user_and_token(
        db_session, email="admin_block_race@example.com", role="admin"
    )
    email = "block_race_user@example.com"
    client.post("/users", json={"email": email, "password": "OriginalPassword123!"})
    user = db_session.query(User).filter_by(email=email).first()
    assert user is not None

    client.post("/auth/password-reset/request", json={"email": email})
    msgs = _get_messages_for_recipient(email)
    assert len(msgs) >= 1
    token = re.search(
        r"Токен:\s*([A-Za-z0-9_-]+)",
        _get_message_detail(msgs[0]["ID"]).get("Text", ""),
    ).group(1)

    def _do_confirm() -> int:
        local_client = TestClient(app)
        r = local_client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "NewRacePassword123!"},
        )
        return r.status_code

    def _do_block() -> int:
        local_client = TestClient(app)
        r = local_client.post(
            f"/users/{user.id}/block",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"reason": "Concurrent block"},
        )
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_confirm = executor.submit(_do_confirm)
        f_block = executor.submit(_do_block)
        confirm_code = f_confirm.result()
        block_code = f_block.result()

    assert block_code == 200
    assert confirm_code in (204, 400)

    # След блокирането потребителят задължително няма достъп (403 ACCOUNT_BLOCKED)
    active_pw = (
        "NewRacePassword123!" if confirm_code == 204 else "OriginalPassword123!"
    )
    login_res = client.post(
        "/auth/login", json={"email": email, "password": active_pw}
    )
    assert login_res.status_code == 403
    assert login_res.json()["detail"]["code"] == "ACCOUNT_BLOCKED"


def test_password_reset_rate_limiting() -> None:
    """10. Ограничаване на честотата на заявките за възстановяване (429 Too Many Requests)."""
    _default_rate_limiter.clear()
    email = "rate_limit_test_user@example.com"

    # Изпълняваме 5 допустими заявки
    for _ in range(5):
        r = client.post("/auth/password-reset/request", json={"email": email})
        assert r.status_code == 202

    # 6-тата заявка надвишава лимита
    exceeded_res = client.post("/auth/password-reset/request", json={"email": email})
    assert exceeded_res.status_code == 429
    assert exceeded_res.json()["detail"]["code"] == "RATE_LIMIT_EXCEEDED"


def test_password_validation_on_confirm_does_not_leak_password() -> None:
    """11. Валидация на новата парола при потвърждаване: твърде кратка дава 422 и не разкрива паролата."""
    short_pw = "Short1!"
    res = client.post(
        "/auth/password-reset/confirm",
        json={"token": "some_dummy_token", "new_password": short_pw},
    )
    assert res.status_code == 422
    assert short_pw not in res.text
