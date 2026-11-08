from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.accounts.models import User
from app.modules.accounts.repository import UserRepository


class EmailAlreadyExists(Exception):
    """Сигнализира, че потребител с този имейл вече съществува."""


class InvalidCredentials(Exception):
    """Сигнализира за невалиден имейл или парола при опит за автентикация."""


class UserNotFound(Exception):
    """Сигнализира, че потребител с посочения идентификатор не съществува."""


class InvalidCurrentPassword(Exception):
    """Сигнализира за грешна текуща парола при опит за смяна на парола."""


class SamePasswordError(Exception):
    """Сигнализира, че новата парола съвпада със старата при опит за смяна."""


# Предварително изчислен валиден Argon2 хеш за предотвратяване на timing attacks при несъществуващ потребител
_DUMMY_HASH: str = PasswordHash.recommended().hash(
    "timing_attack_mitigation_dummy_password"
)


def is_unique_email_violation(exc: IntegrityError) -> bool:
    """Проверява дали грешката е конкретно нарушение на уникалното ограничение uq_users_email."""
    orig = getattr(exc, "orig", None)  # как работи и кьде се намира
    diag = getattr(orig, "diag", None)  # как работи и кьде се намира
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

    def authenticate_user(self, email: str, password: str) -> User:
        """Сценарий: автентикация на потребител с имейл и парола.

        Хвърля InvalidCredentials при несъществуващ потребител или грешна парола.
        При несъществуващ потребител верифицира срещу dummy hash, за да предотврати timing attack.
        """
        user = self._user_repository.get_by_email(email)
        if user is None:
            # Изпълняваме проверка срещу dummy hash, за да изравним времето за отговор
            self._password_hasher.verify(password, _DUMMY_HASH)
            raise InvalidCredentials("Invalid email or password")

        try:
            valid = self._password_hasher.verify(password, user.password_hash)
        except (UnknownHashError, ValueError):
            valid = False

        if not valid:
            raise InvalidCredentials("Invalid email or password")

        return user

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

    def update_user_profile(self, user_id: int, display_name: str | None) -> User:
        """Сценарий: обновяване на профила на потребителя (display_name).

        Управлява жизнения цикъл на транзакцията (commit/rollback).
        Хвърля UserNotFound при несъществуващ потребител.
        """
        user = self._user_repository.get_by_id(user_id)
        if user is None:
            raise UserNotFound(f"User with id {user_id} not found")

        user.display_name = display_name
        try:
            self._session.commit()
            self._session.refresh(user)
            return user
        except Exception:
            self._session.rollback()
            raise

    def change_password(
        self,
        user_id: int,
        current_password: str,
        new_password: str,
    ) -> None:
        """Сценарий: сигурна смяна на паролата на потребител с инвалидиране на съществуващи JWT сесии.

        Заключва реда с FOR UPDATE за предотвратяване на състезателни състояния.
        Верифицира текущата парола (хвърля InvalidCurrentPassword при несъответствие).
        Проверява дали новата парола се различава (хвърля SamePasswordError).
        Хешира новата парола с Argon2, инкрементира token_version с 1 и атомарно потвърждава промените.
        """
        user = self._user_repository.lock_by_id(user_id)
        if user is None:
            raise UserNotFound(f"User with id {user_id} not found")

        try:
            valid = self._password_hasher.verify(current_password, user.password_hash)
        except (UnknownHashError, ValueError):
            valid = False

        if not valid:
            raise InvalidCurrentPassword("Current password is incorrect")

        if current_password == new_password:
            raise SamePasswordError(
                "New password must be different from current password"
            )

        user.password_hash = self._password_hasher.hash(new_password)
        user.token_version += 1

        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
