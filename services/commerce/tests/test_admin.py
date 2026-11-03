import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.modules.accounts.models import User
from scripts.promote_admin import promote_user


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


def test_promote_admin_cli_execution() -> None:
    """Проверка на изпълнението на CLI скрипта promote_admin.py при подаден несъществуващ имейл."""
    script_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "promote_admin.py"
    )
    proc = subprocess.run(
        [sys.executable, str(script_path), "nonexistent_cli_user@example.com"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "не съществува" in proc.stderr
