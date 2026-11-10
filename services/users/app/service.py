"""Бизнес логика (Service слой) за регистрация и автентикация на потребители."""

import logging

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import User
from app.repository import UserRepository

logger = logging.getLogger(__name__)


class EmailAlreadyExists(Exception):
    """Сигнализира, че потребител с този имейл вече съществува."""


class InvalidCredentials(Exception):
    """Сигнализира за невалиден имейл или парола при опит за автентикация."""


class AccountBlocked(Exception):
    """Сигнализира за опит за автентикация на блокиран потребител."""


class AccountClosed(Exception):
    """Сигнализира за опит за автентикация на окончателно закрит профил."""


class UserNotFound(Exception):
    """Сигнализира, че потребител с посочения идентификатор не съществува."""


# Предварително изчислен валиден Argon2 хеш за предотвратяване на timing attacks при несъществуващ потребител
_DUMMY_HASH: str = PasswordHash.recommended().hash(
    "timing_attack_mitigation_dummy_password"
)


def is_unique_email_violation(exc: IntegrityError) -> bool:
    """Проверява дали грешката е конкретно нарушение на уникалното ограничение uq_users_email."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    if diag and getattr(diag, "constraint_name", None) == "uq_users_email":
        return True
    return "uq_users_email" in str(exc)


class UserService:
    """Услуга за регистрация и автентикация на потребителски акаунти."""

    def __init__(
        self,
        user_repository: UserRepository,
        session: Session,
        password_hasher: PasswordHash | None = None,
    ) -> None:
        self._user_repository = user_repository
        self._session = session
        self._password_hasher = password_hasher or PasswordHash.recommended()

    def register_user(self, email: str, password: str) -> User:
        """Сценарий: регистрация на нов потребител (винаги с роля 'customer' и статус 'active').

        Управлява транзакцията (commit/rollback).
        Хвърля EmailAlreadyExists при зает имейл (предварителна проверка или при конкурентно дублиране).
        """
        if self._user_repository.get_by_email(email) is not None:
            raise EmailAlreadyExists(f"User with email '{email}' already exists")

        password_hash = self._password_hasher.hash(password)

        user = User(
            email=email,
            password_hash=password_hash,
            role="customer",
            status="active",
        )

        try:
            self._user_repository.create(user)
            self._session.commit()
            return user
        except IntegrityError as exc:
            self._session.rollback()
            if is_unique_email_violation(exc):
                raise EmailAlreadyExists(
                    f"User with email '{email}' already exists"
                ) from exc
            raise
        except Exception:
            self._session.rollback()
            raise

    def authenticate_user(self, email: str, password: str) -> User:
        """Сценарий: автентикация на потребител с имейл и парола.

        Хвърля InvalidCredentials при несъществуващ потребител или грешна парола.
        Хвърля AccountBlocked или AccountClosed при неактивен профил.
        При несъществуващ потребител верифицира срещу dummy hash, за да предотврати timing attack.
        """
        user = self._user_repository.get_by_email(email)
        if user is None:
            self._password_hasher.verify(password, _DUMMY_HASH)
            raise InvalidCredentials("Invalid email or password")

        try:
            valid = self._password_hasher.verify(password, user.password_hash)
        except (UnknownHashError, ValueError):
            valid = False

        if not valid:
            raise InvalidCredentials("Invalid email or password")

        if user.status == "blocked":
            raise AccountBlocked("Account is blocked")
        if user.status == "closed":
            raise AccountClosed("Account is closed")

        return user
