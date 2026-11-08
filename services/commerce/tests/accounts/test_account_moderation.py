from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.modules.accounts.models import AccountModerationLog, User
from tests.orders.helpers import _create_user_and_token

client = TestClient(app)


def _create_admin(db_session: Session, email: str = "admin@example.com") -> tuple[User, str]:
    """Създава потребител с администраторска роля (admin) и валиден токен."""
    return _create_user_and_token(db_session, email=email, role="admin")


def test_admin_block_and_unblock_cycle_and_token_invalidation(
    db_session: Session,
) -> None:
    """1. Блокирането незабавно прекратява достъпа по стария JWT и забранява нов вход.

    След отблокиране потребителят може да влезе отново, но старите токени не оживяват.
    """
    admin, admin_token = _create_admin(db_session)

    user_email = "target_block_user@example.com"
    user_pass = "UserPassword12345!"
    client.post("/users", json={"email": user_email, "password": user_pass})
    login_res = client.post(
        "/auth/login", json={"email": user_email, "password": user_pass}
    )
    user_id = login_res.json()
    user_old_token = login_res.json()["access_token"]

    # Преди блока потребителят има нормален достъп
    pre_res = client.get(
        "/users/me", headers={"Authorization": f"Bearer {user_old_token}"}
    )
    assert pre_res.status_code == 200

    target_user = db_session.query(User).filter_by(email=user_email).first()
    assert target_user is not None

    # 1. Администраторът блокира потребителя
    block_res = client.post(
        f"/users/{target_user.id}/block",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "Spam and suspicious activity"},
    )
    assert block_res.status_code == 200
    assert block_res.json()["status"] == "blocked"

    # 2. Старият JWT веднага получава 401
    post_block_res = client.get(
        "/users/me", headers={"Authorization": f"Bearer {user_old_token}"}
    )
    assert post_block_res.status_code == 401

    # 3. Нов вход е забранен (403)
    login_blocked_res = client.post(
        "/auth/login", json={"email": user_email, "password": user_pass}
    )
    assert login_blocked_res.status_code == 403
    assert login_blocked_res.json()["detail"]["code"] == "ACCOUNT_BLOCKED"

    # 4. Проверка на записа в журнала за модерация (account_moderation_logs)
    log_stmt = select(AccountModerationLog).where(
        AccountModerationLog.user_id == target_user.id,
        AccountModerationLog.action == "blocked",
    )
    log_entry = db_session.scalars(log_stmt).first()
    assert log_entry is not None
    assert log_entry.admin_id == admin.id
    assert log_entry.reason == "Spam and suspicious activity"
    assert log_entry.created_at is not None

    # 5. Администраторът отблокира потребителя
    unblock_res = client.post(
        f"/users/{target_user.id}/unblock",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "Resolved issue with user"},
    )
    assert unblock_res.status_code == 200
    assert unblock_res.json()["status"] == "active"

    # 6. Старият JWT издаден ПРЕДИ блока НЕ оживява (401)
    old_jwt_revival_res = client.get(
        "/users/me", headers={"Authorization": f"Bearer {user_old_token}"}
    )
    assert old_jwt_revival_res.status_code == 401

    # 7. Потребителят може да влезе наново и да получи нов валиден JWT
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
    db_session: Session,
) -> None:
    """2. Закриването на акаунт е окончателно, редът се запазва физически и не може да се отблокира."""
    admin, admin_token = _create_admin(db_session)
    user, _ = _create_user_and_token(
        db_session, email="close_test_user@example.com"
    )

    # 1. Закриване на акаунта
    close_res = client.post(
        f"/users/{user.id}/close",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "Account closed permanently upon request"},
    )
    assert close_res.status_code == 200
    assert close_res.json()["status"] == "closed"

    # 2. Потребителският ред остава физически в базата данни
    db_session.expire_all()
    db_user = db_session.get(User, user.id)
    assert db_user is not None
    assert db_user.status == "closed"

    # 3. Опит за отблокиране се отхвърля с 400
    unblock_attempt = client.post(
        f"/users/{user.id}/unblock",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert unblock_attempt.status_code == 400
    assert unblock_attempt.json()["detail"]["code"] == "INVALID_ACCOUNT_STATE"

    # 4. Повторен опит за блокиране или закриване също се отхвърля
    reclose_attempt = client.post(
        f"/users/{user.id}/close",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert reclose_attempt.status_code == 400
    assert reclose_attempt.json()["detail"]["code"] == "INVALID_ACCOUNT_STATE"


def test_self_moderation_forbidden(db_session: Session) -> None:
    """3. Администраторът не може да блокира или закрива собствения си акаунт (400)."""
    admin, admin_token = _create_admin(
        db_session, email="self_mod_admin@example.com"
    )

    # Самоблокиране
    r1 = client.post(
        f"/users/{admin.id}/block",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r1.status_code == 400
    assert r1.json()["detail"]["code"] == "SELF_MODERATION_FORBIDDEN"

    # Самозакриване
    r2 = client.post(
        f"/users/{admin.id}/close",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 400
    assert r2.json()["detail"]["code"] == "SELF_MODERATION_FORBIDDEN"


def test_technical_account_moderation_protected(db_session: Session) -> None:
    """4. Служебният технически акаунт на каталога е защитен от модерация (400)."""
    _, admin_token = _create_admin(db_session)

    tech_user = (
        db_session.query(User)
        .filter_by(email="legacy-catalog@internal.ecommerce.com")
        .first()
    )
    assert tech_user is not None

    # Опит за блокиране
    r1 = client.post(
        f"/users/{tech_user.id}/block",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r1.status_code == 400
    assert r1.json()["detail"]["code"] == "TECHNICAL_ACCOUNT_PROTECTED"

    # Опит за закриване
    r2 = client.post(
        f"/users/{tech_user.id}/close",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 400
    assert r2.json()["detail"]["code"] == "TECHNICAL_ACCOUNT_PROTECTED"


def test_non_admin_cannot_access_moderation(db_session: Session) -> None:
    """5. Обикновен потребител няма достъп до модерационните маршрути (403)."""
    user, user_token = _create_user_and_token(
        db_session, email="regular_mod_user@example.com"
    )

    r_block = client.post(
        f"/users/{user.id}/block",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r_block.status_code == 403
    assert r_block.json()["detail"]["code"] == "FORBIDDEN"

    r_unblock = client.post(
        f"/users/{user.id}/unblock",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r_unblock.status_code == 403

    r_close = client.post(
        f"/users/{user.id}/close",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r_close.status_code == 403


def test_patch_users_me_cannot_change_role_or_status(
    db_session: Session,
) -> None:
    """6. През PATCH /users/me не може да се сменя роля или статус (422)."""
    _, user_token = _create_user_and_token(
        db_session, email="patch_tamper_user@example.com"
    )

    r_role = client.patch(
        "/users/me",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"role": "admin"},
    )
    assert r_role.status_code == 422

    r_status = client.patch(
        "/users/me",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"status": "blocked"},
    )
    assert r_status.status_code == 422
