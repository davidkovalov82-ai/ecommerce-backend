from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.accounts.models import User
from app.modules.accounts.repository import UserRepository


class EmailAlreadyExists(Exception):
    """Сигнализира, че потребител с този имейл вече съществува."""


def is_unique_email_violation(exc: IntegrityError) -> bool:
    """Проверява дали грешката е конкретно нарушение на уникалното ограничение uq_users_email."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    if diag and getattr(diag, "constraint_name", None) == "uq_users_email":
        return True
    return "uq_users_email" in str(exc)


class AccountService:
    """Услуга за управление на потребителски акаунти и автентикация."""

    def __init__(
        self,
        user_repository: UserRepository,
        session: Session,
        password_hasher: PasswordHash,
    ) -> None:
        self._user_repository = user_repository
        self._session = session
        self._password_hasher = password_hasher

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """Проверява съвпадението на предоставена парола спрямо съхранен хеш."""
        return self._password_hasher.verify(plain_password, hashed_password)

    def register_user(self, email: str, password: str) -> User:
        """Сценарий: регистрация на нов потребител.

        Управлява жизнения цикъл на транзакцията (commit/rollback).
        Хвърля EmailAlreadyExists при зает имейл (предварителна проверка или при конкурентно дублиране).
        """
        # 1. Предварителна проверка за зает имейл
        if self._user_repository.get_by_email(email) is not None:
            raise EmailAlreadyExists(f"User with email '{email}' already exists")

        # 2. Сигурно хеширане на паролата с Argon2
        password_hash = self._password_hasher.hash(password)

        # 3. Регистрацията винаги създава потребител с роля 'customer'
        user = User(
            email=email,
            password_hash=password_hash,
            role="customer",
        )

        # 4. Запис и атомарно потвърждаване на транзакцията
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
