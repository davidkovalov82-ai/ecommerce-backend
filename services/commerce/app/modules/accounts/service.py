from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.accounts import repository
from app.modules.accounts.models import User

# Използваме препоръчаните настройки за Argon2 от pwdlib
password_hasher = PasswordHash.recommended()


class EmailAlreadyExists(Exception):
    """Сигнализира, че потребител с този имейл вече съществува."""


def is_unique_email_violation(exc: IntegrityError) -> bool:
    """Проверява дали грешката е конкретно нарушение на уникалното ограничение uq_users_email."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    if diag and getattr(diag, "constraint_name", None) == "uq_users_email":
        return True
    return "uq_users_email" in str(exc)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверява съвпадението на предоставена парола спрямо съхранен хеш."""
    return password_hasher.verify(plain_password, hashed_password)


def register_user(session: Session, email: str, password: str) -> User:
    """Сценарий: регистрация на нов потребител.

    Управлява жизнения цикъл на транзакцията (commit/rollback).
    Хвърля EmailAlreadyExists при зает имейл (предварителна проверка или при конкурентно дублиране).
    """
    # 1. Предварителна проверка за зает имейл
    if repository.get_by_email(session, email) is not None:
        raise EmailAlreadyExists(f"User with email '{email}' already exists")

    # 2. Сигурно хеширане на паролата с Argon2
    password_hash = password_hasher.hash(password)

    # 3. Регистрацията винаги създава потребител с роля 'customer'
    user = User(
        email=email,
        password_hash=password_hash,
        role="customer",
    )

    # 4. Запис и атомарно потвърждаване на транзакцията
    try:
        repository.create(session, user)
        session.commit()
        return user
    except IntegrityError as exc:
        session.rollback()
        if is_unique_email_violation(exc):
            raise EmailAlreadyExists(
                f"User with email '{email}' already exists"
            ) from exc
        raise
    except Exception:
        session.rollback()
        raise
