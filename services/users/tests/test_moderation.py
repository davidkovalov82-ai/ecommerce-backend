"""Интеграционни тестове за административна модерация: block, unblock, close."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountModerationLog, User
from tests.conftest import create_admin, create_user_and_token


def test_admin_block_and_unblock_cycle_and_token_invalidation(
    client: TestClient, db_session: Session
) -> None:
    """Блокирането незабавно прекратява достъпа със стария токен и забранява вход.

    След отблокиране новият вход работи, но старите токени не оживяват.
    """
    admin, admin_token = create_admin(db_session)
    user_email = "target_block_user@example.com"
    user_pass = "UserSecurePassword123!"

    target_user, user_old_token = create_user_and_token(
        db_session, email=user_email, password=user_pass
    )

    # 1. Преди блокиране потребителят има нормален достъп
    pre_res = client.get(
        "/users/me", headers={"Authorization": f"Bearer {user_old_token}"}
    )
    assert pre_res.status_code == 200

    # 2. Администраторът блокира потребителя
    block_res = client.post(
        f"/users/{target_user.id}/block",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "Spam activity detected"},
    )
    assert block_res.status_code == 200
    assert block_res.json()["status"] == "blocked"

    # 3. Старият JWT веднага връща 401
    post_block_res = client.get(
        "/users/me", headers={"Authorization": f"Bearer {user_old_token}"}
    )
    assert post_block_res.status_code == 401

    # 4. Нов вход е забранен с 403 ACCOUNT_BLOCKED
    login_blocked_res = client.post(
        "/auth/login", json={"email": user_email, "password": user_pass}
    )
    assert login_blocked_res.status_code == 403
    assert login_blocked_res.json()["detail"]["code"] == "ACCOUNT_BLOCKED"

    # 5. Проверка на записа в одит лога
    log_stmt = select(AccountModerationLog).where(
        AccountModerationLog.user_id == target_user.id,
        AccountModerationLog.action == "blocked",
    )
    log_entry = db_session.scalars(log_stmt).first()
    assert log_entry is not None
    assert log_entry.admin_id == admin.id
    assert log_entry.reason == "Spam activity detected"

    # 6. Администраторът отблокира потребителя
    unblock_res = client.post(
        f"/users/{target_user.id}/unblock",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "Resolved by support"},
    )
    assert unblock_res.status_code == 200
    assert unblock_res.json()["status"] == "active"

    # 7. Старият JWT, издаден преди блока, НЕ оживява (401)
    old_jwt_revival = client.get(
        "/users/me", headers={"Authorization": f"Bearer {user_old_token}"}
    )
    assert old_jwt_revival.status_code == 401

    # 8. Потребителят може да влезе наново и да получи нов валиден JWT
    re_login_res = client.post(
        "/auth/login", json={"email": user_email, "password": user_pass}
    )
    assert re_login_res.status_code == 200
    new_token = re_login_res.json()["access_token"]

    new_jwt_res = client.get(
        "/users/me", headers={"Authorization": f"Bearer {new_token}"}
    )
    assert new_jwt_res.status_code == 200
    assert new_jwt_res.json()["email"] == user_email


def test_admin_close_account_is_irreversible_and_preserves_row(
    client: TestClient, db_session: Session
) -> None:
    """Закриването на акаунт е окончателно: редът се запазва физически, отблокирането е невъзможно."""
    _admin, admin_token = create_admin(db_session)
    user, user_token = create_user_and_token(
        db_session, email="close_target@example.com"
    )

    # 1. Закриване на акаунта
    close_res = client.post(
        f"/users/{user.id}/close",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "GDPR deletion request"},
    )
    assert close_res.status_code == 200
    assert close_res.json()["status"] == "closed"

    # 2. Потребителският ред остава физически в базата данни
    db_session.expire_all()
    db_user = db_session.get(User, user.id)
    assert db_user is not None
    assert db_user.status == "closed"

    # 3. Старият токен се отхвърля
    assert (
        client.get(
            "/users/me", headers={"Authorization": f"Bearer {user_token}"}
        ).status_code
        == 401
    )

    # 4. Опит за отблокиране се отхвърля с 400 INVALID_ACCOUNT_STATE
    unblock_attempt = client.post(
        f"/users/{user.id}/unblock",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert unblock_attempt.status_code == 400
    assert unblock_attempt.json()["detail"]["code"] == "INVALID_ACCOUNT_STATE"

    # 5. Повторно закриване се отхвърля с 400
    reclose_attempt = client.post(
        f"/users/{user.id}/close",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert reclose_attempt.status_code == 400
    assert reclose_attempt.json()["detail"]["code"] == "INVALID_ACCOUNT_STATE"


def test_non_admin_cannot_access_moderation(
    client: TestClient, db_session: Session
) -> None:
    """Обикновен потребител (customer) няма достъп до административните маршрути (403 FORBIDDEN)."""
    user, user_token = create_user_and_token(
        db_session, email="regular_mod@example.com"
    )
    headers = {"Authorization": f"Bearer {user_token}"}

    for action in ["block", "unblock", "close"]:
        res = client.post(f"/users/{user.id}/{action}", headers=headers)
        assert res.status_code == 403
        assert res.json()["detail"]["code"] == "FORBIDDEN"


def test_self_moderation_forbidden(client: TestClient, db_session: Session) -> None:
    """Администраторът не може да блокира или закрива собствения си акаунт (400 SELF_MODERATION_FORBIDDEN)."""
    admin, admin_token = create_admin(db_session, email="self_mod_adm@example.com")
    headers = {"Authorization": f"Bearer {admin_token}"}

    # Самоблокиране
    r1 = client.post(f"/users/{admin.id}/block", headers=headers)
    assert r1.status_code == 400
    assert r1.json()["detail"]["code"] == "SELF_MODERATION_FORBIDDEN"

    # Самозакриване
    r2 = client.post(f"/users/{admin.id}/close", headers=headers)
    assert r2.status_code == 400
    assert r2.json()["detail"]["code"] == "SELF_MODERATION_FORBIDDEN"


def test_technical_account_protected(client: TestClient, db_session: Session) -> None:
    """Служебният технически акаунт е защитен от модерация (400 TECHNICAL_ACCOUNT_PROTECTED)."""
    _admin, admin_token = create_admin(db_session)
    tech_user, _token = create_user_and_token(
        db_session, email="legacy-catalog@internal.ecommerce.com"
    )
    headers = {"Authorization": f"Bearer {admin_token}"}

    r_block = client.post(f"/users/{tech_user.id}/block", headers=headers)
    assert r_block.status_code == 400
    assert r_block.json()["detail"]["code"] == "TECHNICAL_ACCOUNT_PROTECTED"

    r_close = client.post(f"/users/{tech_user.id}/close", headers=headers)
    assert r_close.status_code == 400
    assert r_close.json()["detail"]["code"] == "TECHNICAL_ACCOUNT_PROTECTED"
