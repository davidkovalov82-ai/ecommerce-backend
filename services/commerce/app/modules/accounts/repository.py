import datetime
from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.modules.accounts.models import AccountModerationLog, PasswordResetToken, User


class UserRepository:
    """Хранилище за достъп до данни за потребители, токени за възстановяване и одит логове."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_email(self, email: str) -> User | None:
        """Търси потребител по нормализиран имейл адрес."""
        stmt = select(User).where(User.email == email)
        return self._session.scalars(stmt).first()

    def get_by_id(self, user_id: int) -> User | None:
        """Извлича потребител по ID."""
        return self._session.get(User, user_id)

    def get_by_ids(self, user_ids: Sequence[int]) -> Sequence[User]:
        """Извлича множество потребители по списък от идентификатори."""
        if not user_ids:
            return []
        stmt = select(User).where(User.id.in_(user_ids))
        return self._session.scalars(stmt).all()

    def lock_by_id(self, user_id: int) -> User | None:
        """Заключва реда на потребителя с FOR UPDATE и опреснява данните му от БД."""
        stmt = (
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def create(self, user: User) -> User:
        """Добавя нов потребител в сесията без да изпълнява commit.

        Транзакцията и commit/rollback се управляват от Service слоя.
        """
        self._session.add(user)
        self._session.flush()
        return user

    def create_password_reset_token(
        self, token_record: PasswordResetToken
    ) -> PasswordResetToken:
        """Записва нов хеширан токен за възстановяване на парола."""
        self._session.add(token_record)
        self._session.flush()
        return token_record

    def get_password_reset_token_for_update(
        self, token_hash: str
    ) -> PasswordResetToken | None:
        """Извлича и заключва запис на токен за възстановяване по неговия хеш."""
        stmt = (
            select(PasswordResetToken)
            .where(PasswordResetToken.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def invalidate_user_reset_tokens(self, user_id: int) -> None:
        """Инвалидира всички неизползвани токени за възстановяване на даден потребител."""
        now = datetime.datetime.now(datetime.UTC)
        stmt = (
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
            )
            .values(used_at=now)
        )
        self._session.execute(stmt)
        self._session.flush()

    def create_moderation_log(
        self, log_entry: AccountModerationLog
    ) -> AccountModerationLog:
        """Записва събитие в одитния журнал за модерация."""
        self._session.add(log_entry)
        self._session.flush()
        return log_entry
