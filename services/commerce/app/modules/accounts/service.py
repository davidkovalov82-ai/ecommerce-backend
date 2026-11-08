import datetime
import hashlib
import secrets
import time

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.accounts.mailer import AccountMailer
from app.modules.accounts.models import AccountModerationLog, PasswordResetToken, User
from app.modules.accounts.repository import UserRepository

_TECHNICAL_ACCOUNT_EMAIL = "legacy-catalog@internal.ecommerce.com"


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


class AccountBlocked(Exception):
    """Сигнализира за опит за автентикация на блокиран потребител."""


class AccountClosed(Exception):
    """Сигнализира за опит за автентикация на окончателно закрит профил."""


class RateLimitExceeded(Exception):
    """Сигнализира за превишен лимит на честотата на заявките."""


class InvalidResetToken(Exception):
    """Сигнализира за невалиден, изтекъл или вече използван токен за възстановяване на парола."""


class SelfModerationForbidden(Exception):
    """Сигнализира за недопустим опит за самомодерация от администратор."""


class TechnicalAccountProtected(Exception):
    """Сигнализира за недопустим опит за модерация на служебен системен профил."""


class InvalidAccountState(Exception):
    """Сигнализира за невалидно текущо състояние на потребителя за исканата модерация."""


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


class PasswordResetRateLimiter:
    """Ограничител на честотата на заявките за възстановяване на парола (плъзгащ се прозорец)."""

    def __init__(self, max_requests: int = 5, window_seconds: float = 900.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._history: dict[str, list[float]] = {}

    def check_and_record(self, key: str, current_time: float | None = None) -> bool:
        """Връща True ако заявката е позволена и записва опита, иначе False."""
        now = current_time if current_time is not None else time.time()
        cutoff = now - self.window_seconds
        timestamps = [t for t in self._history.get(key, []) if t > cutoff]
        if len(timestamps) >= self.max_requests:
            self._history[key] = timestamps
            return False
        timestamps.append(now)
        self._history[key] = timestamps
        return True

    def clear(self) -> None:
        """Изчиства историята на заявките."""
        self._history.clear()


_default_rate_limiter = PasswordResetRateLimiter()


class AccountService:
    """Услуга за управление на потребителски акаунти, автентикация, модерация и възстановяване на пароли."""

    def __init__(
        self,
        user_repository: UserRepository,
        session: Session,
        password_hasher: PasswordHash,
        mailer: AccountMailer | None = None,
        rate_limiter: PasswordResetRateLimiter | None = None,
    ) -> None:
        self._user_repository = user_repository
        self._session = session
        self._password_hasher = password_hasher
        self._mailer = mailer or AccountMailer()
        self._rate_limiter = rate_limiter or _default_rate_limiter

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """Проверява съвпадението на предоставена парола спрямо съхранен хеш."""
        return self._password_hasher.verify(plain_password, hashed_password)

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

    def register_user(self, email: str, password: str) -> User:
        """Сценарий: регистрация на нов потребител.

        Управлява жизнения цикъл на транзакцията (commit/rollback).
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

    def request_password_reset(
        self, email: str, client_ip: str | None = None
    ) -> None:
        """Сценарий: заявка за възстановяване на забравена парола по имейл.

        Ограничава честотата на заявките по IP и имейл без разкриване на съществуването на акаунта.
        При несъществуващ, блокиран, закрит или служебен технически акаунт не изпраща писмо,
        но завършва без грешка (за идентичен отговор 202).
        """
        if client_ip and not self._rate_limiter.check_and_record(f"ip:{client_ip}"):
            raise RateLimitExceeded(
                "Too many password reset requests. Please try again later."
            )
        if not self._rate_limiter.check_and_record(f"email:{email.lower()}"):
            raise RateLimitExceeded(
                "Too many password reset requests. Please try again later."
            )

        user = self._user_repository.get_by_email(email)
        if user is None:
            return

        if (
            user.email == _TECHNICAL_ACCOUNT_EMAIL
            or user.password_hash.startswith("!")
        ):
            return

        if user.status != "active":
            return

        plain_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(plain_token.encode("utf-8")).hexdigest()
        expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            minutes=settings.password_reset_expire_minutes
        )

        token_record = PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        try:
            self._user_repository.create_password_reset_token(token_record)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        reset_url = f"{settings.password_reset_base_url}?token={plain_token}"
        self._mailer.send_password_reset_email(
            to_email=user.email,
            token=plain_token,
            reset_url=reset_url,
        )

    def confirm_password_reset(self, token: str, new_password: str) -> None:
        """Сценарий: потвърждаване на възстановяване на парола с еднократен токен.

        Атомарно в една транзакция:
        1. Заключва записа на токена с FOR UPDATE (само един конкурентен опит успява).
        2. Верифицира валидността (неизползван и неизтекъл).
        3. Заключва потребителя и проверява дали е активен.
        4. Задава новата парола, инкрементира token_version и маркира токена като използван.
        5. Инвалидира всички останали неизползвани токени на потребителя.
        """
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        token_record = self._user_repository.get_password_reset_token_for_update(
            token_hash
        )
        now = datetime.datetime.now(datetime.UTC)

        if (
            token_record is None
            or token_record.used_at is not None
            or token_record.expires_at < now
        ):
            raise InvalidResetToken("Invalid or expired password reset token")

        user = self._user_repository.lock_by_id(token_record.user_id)
        if user is None or user.status != "active":
            raise InvalidResetToken("Invalid or expired password reset token")

        token_record.used_at = now
        self._user_repository.invalidate_user_reset_tokens(user.id)
        user.password_hash = self._password_hasher.hash(new_password)
        user.token_version += 1

        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def block_user(
        self, admin_id: int, target_user_id: int, reason: str | None = None
    ) -> User:
        """Сценарий: администратор блокира потребителски акаунт.

        Прекратява незабавно активните сесии (token_version += 1) и забранява нови влизания.
        Защитава от самоблокиране и модерация на служебния технически акаунт.
        """
        if admin_id == target_user_id:
            raise SelfModerationForbidden("Cannot moderate your own account")

        target_user = self._user_repository.lock_by_id(target_user_id)
        if target_user is None:
            raise UserNotFound(f"User with id {target_user_id} not found")

        if (
            target_user.email == _TECHNICAL_ACCOUNT_EMAIL
            or target_user.password_hash.startswith("!")
        ):
            raise TechnicalAccountProtected(
                "Cannot moderate technical system account"
            )

        if target_user.status == "closed":
            raise InvalidAccountState("Cannot block an already closed account")

        target_user.status = "blocked"
        target_user.token_version += 1
        self._user_repository.invalidate_user_reset_tokens(target_user.id)

        log_entry = AccountModerationLog(
            user_id=target_user.id,
            admin_id=admin_id,
            action="blocked",
            reason=reason,
        )
        self._user_repository.create_moderation_log(log_entry)

        try:
            self._session.commit()
            return target_user
        except Exception:
            self._session.rollback()
            raise

    def unblock_user(
        self, admin_id: int, target_user_id: int, reason: str | None = None
    ) -> User:
        """Сценарий: администратор отблокира потребителски акаунт.

        Увеличава token_version, за да гарантира, че сесии отпреди блока не се съживяват.
        Закрити акаунти не могат да бъдат отблокирани (закриването е окончателно).
        """
        target_user = self._user_repository.lock_by_id(target_user_id)
        if target_user is None:
            raise UserNotFound(f"User with id {target_user_id} not found")

        if (
            target_user.email == _TECHNICAL_ACCOUNT_EMAIL
            or target_user.password_hash.startswith("!")
        ):
            raise TechnicalAccountProtected(
                "Cannot moderate technical system account"
            )

        if target_user.status == "closed":
            raise InvalidAccountState("Closed accounts cannot be unblocked")

        if target_user.status == "active":
            raise InvalidAccountState("Account is already active")

        target_user.status = "active"
        target_user.token_version += 1
        self._user_repository.invalidate_user_reset_tokens(target_user.id)

        log_entry = AccountModerationLog(
            user_id=target_user.id,
            admin_id=admin_id,
            action="unblocked",
            reason=reason,
        )
        self._user_repository.create_moderation_log(log_entry)

        try:
            self._session.commit()
            return target_user
        except Exception:
            self._session.rollback()
            raise

    def close_user(
        self, admin_id: int, target_user_id: int, reason: str | None = None
    ) -> User:
        """Сценарий: администратор окончателно закрива потребителски акаунт.

        Редът в базата се запазва физически за съхранение на исторически поръчки и плащания.
        Действието е необратимо.
        """
        if admin_id == target_user_id:
            raise SelfModerationForbidden("Cannot moderate your own account")

        target_user = self._user_repository.lock_by_id(target_user_id)
        if target_user is None:
            raise UserNotFound(f"User with id {target_user_id} not found")

        if (
            target_user.email == _TECHNICAL_ACCOUNT_EMAIL
            or target_user.password_hash.startswith("!")
        ):
            raise TechnicalAccountProtected(
                "Cannot moderate technical system account"
            )

        if target_user.status == "closed":
            raise InvalidAccountState("Account is already closed")

        target_user.status = "closed"
        target_user.token_version += 1
        self._user_repository.invalidate_user_reset_tokens(target_user.id)

        log_entry = AccountModerationLog(
            user_id=target_user.id,
            admin_id=admin_id,
            action="closed",
            reason=reason,
        )
        self._user_repository.create_moderation_log(log_entry)

        try:
            self._session.commit()
            return target_user
        except Exception:
            self._session.rollback()
            raise
