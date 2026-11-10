"""Интеграционен тест за възстановяване на парола през реален Mailpit (без mock на AccountMailer)."""

import json
import os
import re
import time
import urllib.request
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.service import _default_rate_limiter


def test_password_reset_flow_with_real_mailpit(
    client: TestClient, db_session: Session
) -> None:
    """Проверява пълния цикъл на възстановяване на парола през реалния Mailpit SMTP и REST API.

    - Регистрация и успешен първоначален вход.
    - Заявка за възстановяване без подмяна на AccountMailer.
    - Изчакване на писмото в Mailpit с ограничен таймаут (до 10s).
    - Извличане на токена от съобщението и потвърждаване с нова парола.
    - Потвърждение, че новата парола работи, а старата се отхвърля.
    - Отхвърляне на стария access-токен и на повторно прилагане на същия reset-токен.
    - Изолирани данни: уникален имейл адрес, без изтриване на чужди съобщения в Mailpit.
    """
    _default_rate_limiter.clear()

    mailpit_web_url = os.environ.get("MAILPIT_WEB_URL", "http://127.0.0.1:8025")
    unique_id = uuid.uuid4().hex[:10]
    email = f"mailpit_user_{unique_id}@example.com"
    old_password = "InitialSecurePassword123!"
    new_password = "NewBrandSecurePassword456!"

    # 1. Регистрация на тестов потребител
    reg_res = client.post("/users", json={"email": email, "password": old_password})
    assert reg_res.status_code == 201

    login_res = client.post(
        "/auth/login", json={"email": email, "password": old_password}
    )
    assert login_res.status_code == 200
    old_access_token = login_res.json()["access_token"]

    # 2. Заявка за възстановяване на парола през истински AccountMailer
    req_res = client.post("/auth/password-reset/request", json={"email": email})
    assert req_res.status_code == 202
    assert req_res.json() == {
        "message": "If the account exists and is eligible, password reset instructions have been sent."
    }

    # 3. Изчакване на съобщението в Mailpit с ограничен таймаут
    messages: list[dict[str, object]] = []
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{mailpit_web_url}/api/v1/messages")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                messages = [
                    m
                    for m in data.get("messages", [])
                    if any(to.get("Address") == email for to in m.get("To", []))
                ]
                if messages:
                    break
        except OSError:
            pass
        time.sleep(0.3)

    assert messages, "Email did not arrive in Mailpit within 10 seconds timeout"

    # 4. Извличане на токена от тялото на писмото
    message_id = str(messages[0]["ID"])
    detail_req = urllib.request.Request(
        f"{mailpit_web_url}/api/v1/message/{message_id}"
    )
    with urllib.request.urlopen(detail_req, timeout=2) as resp:
        detail = json.loads(resp.read().decode("utf-8"))

    body_text = str(detail.get("Text", ""))
    match = re.search(r"Токен:\s*([A-Za-z0-9_-]+)", body_text)
    assert match is not None, "Reset token not found in email body"
    reset_token = match.group(1)

    # 5. Потвърждение на новата парола с извлечения токен
    confirm_res = client.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": new_password},
    )
    assert confirm_res.status_code == 204

    # 6. Проверка: новата парола работи, старата — не
    failed_login = client.post(
        "/auth/login", json={"email": email, "password": old_password}
    )
    assert failed_login.status_code == 401

    new_login = client.post(
        "/auth/login", json={"email": email, "password": new_password}
    )
    assert new_login.status_code == 200
    assert "access_token" in new_login.json()

    # 7. Проверка: старият access-токен се отхвърля
    old_jwt_res = client.get(
        "/users/me", headers={"Authorization": f"Bearer {old_access_token}"}
    )
    assert old_jwt_res.status_code == 401
    assert old_jwt_res.json()["detail"]["code"] == "INVALID_TOKEN"

    # 8. Проверка: повторно прилагане на същия reset-токен се отхвърля
    re_confirm_res = client.post(
        "/auth/password-reset/confirm",
        json={"token": reset_token, "new_password": "YetAnotherValidPassword789!"},
    )
    assert re_confirm_res.status_code == 400
    assert re_confirm_res.json()["detail"]["code"] == "INVALID_RESET_TOKEN"
