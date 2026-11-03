import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.accounts.models import User
from scripts.promote_admin import promote_user
from tests.conftest import TEST_DB_NAME

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "promote_admin.py"
SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "promote_admin.py"
)


def _get_cli_test_env() -> dict[str, str]:
    """Връща среда за subprocess, насочваща към изолираната тестова база commerce_test."""
    env = os.environ.copy()
    env.update(
        {
            "DB_HOST": settings.db_host,
            "DB_PORT": str(settings.db_port),
            "DB_USER": settings.db_user,
            "DB_PASSWORD": settings.db_password,
            "DB_NAME": TEST_DB_NAME,
            "JWT_SECRET": settings.jwt_secret,
        }
    )
    return env


def test_promote_user_normalizes_email_and_preserves_password(
    db_session: Session,
) -> None:
    """CLI функцията promote_user нормализира имейл, променя ролята на admin и запазва паролата."""
    original_hash = "$argon2id$v=19$m=65536,t=3,p=4$some_random_hash"
    user = User(
        email="promote_me@example.com",
        password_hash=original_hash,
        role="customer",
    )
    db_session.add(user)
    db_session.commit()

    # Повишаване с главни букви и интервали
    changed = promote_user(db_session, "  PROMOTE_ME@EXAMPLE.COM  ")
    assert changed is True

    # Проверка на записа в базата
    db_session.refresh(user)
    assert user.role == "admin"
    assert user.password_hash == original_hash  # Паролата НЕ е променена!


def test_promote_user_idempotent_on_already_admin(db_session: Session) -> None:
    """Повторното изпълнение за потребител с роля admin е безопасно и връща False."""
    user = User(
        email="already_admin@example.com",
        password_hash="some_hash",
        role="admin",
    )
    db_session.add(user)
    db_session.commit()

    changed = promote_user(db_session, "already_admin@example.com")
    assert changed is False
    assert user.role == "admin"


def test_promote_user_missing_user_raises_value_error(db_session: Session) -> None:
    """При несъществуващ потребител promote_user повдига ValueError."""
    with pytest.raises(ValueError, match="не съществува"):
        promote_user(db_session, "nonexistent_admin@example.com")


def test_promote_user_empty_email_raises_value_error(db_session: Session) -> None:
    """При празен имейл promote_user повдига ValueError."""
    with pytest.raises(ValueError, match="не може да бъде празен"):
        promote_user(db_session, "   ")


def test_cli_missing_user_returns_exit_code_1() -> None:
    """Реален subprocess: при несъществуващ потребител promote_admin.py връща exit code 1."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "missing_cli_user@example.com"],
        env=_get_cli_test_env(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 1
    assert "не съществува" in proc.stderr


def test_cli_promotes_existing_customer_and_is_safe_on_repeat(
    db_session: Session,
) -> None:
    """Реален subprocess срещу тестовата БД: повишава customer до admin, а повторението е безопасно."""
    original_hash = "$argon2id$v=19$m=65536,t=3,p=4$cli_safe_password_hash"
    user = User(
        email="cli_customer@example.com",
        password_hash=original_hash,
        role="customer",
    )
    db_session.add(user)
    db_session.commit()

    # 1. Първо изпълнение през subprocess с интервали и главни букви
    proc1 = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "  CLI_CUSTOMER@EXAMPLE.COM  "],
        env=_get_cli_test_env(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc1.returncode == 0
    assert "успешно повишен" in proc1.stdout

    # Прочитаме актуалното състояние директно от тестовата БД
    db_session.expire_all()
    db_session.refresh(user)
    assert user.role == "admin"
    assert user.password_hash == original_hash

    # 2. Повторно изпълнение през subprocess за вече съществуващ admin
    proc2 = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "cli_customer@example.com"],
        env=_get_cli_test_env(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc2.returncode == 0
    assert "вече има роля 'admin'" in proc2.stdout

    # Прочитаме отново състоянието от тестовата БД: ролята и хешът не са променени
    db_session.expire_all()
    db_session.refresh(user)
    assert user.role == "admin"
    assert user.password_hash == original_hash
