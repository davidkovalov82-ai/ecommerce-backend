"""Слой за достъп до данни (Repository) за потребителите, токените и одит логовете."""

import datetime

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.models import AccountModerationLog, PasswordResetToken, User


class UserRepository:
    """Хранилище за изпълнение на SQL заявки към таблицата users и свързаните записи."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, user_id: int) -> User | None:
        """Връща потребител по ID или None."""
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        """Връща потребител по нормализиран имейл или None."""
        stmt = select(User).where(User.email == email)
        return self._session.scalar(stmt)

    def create(self, user: User) -> User:
        """Добавя потребител в текущата сесия и прави flush."""
        self._session.add(user)
        self._session.flush()
        return user

    def lock_by_id(self, user_id: int) -> User | None:
        """Заключва реда на потребителя с FOR UPDATE и опреснява данните му от БД."""
        stmt = (
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(stmt)

    def create_password_reset_token(
        self, token_record: PasswordResetToken
    ) -> PasswordResetToken:
        """Записва нов хеширан токен за възстановяване на парола."""
        self._session.add(token_record)
        self._session.flush()
        return token_record

    def get_password_reset_token_by_hash(
        self, token_hash: str
    ) -> PasswordResetToken | None:
        """Извлича запис на токен за възстановяване по неговия хеш без заключване."""
        stmt = select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash
        )
        return self._session.scalar(stmt)

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
        return self._session.scalar(stmt)

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

    def sync_sequences(self) -> None:
        """Синхронизира PostgreSQL sequence броячите, без да ги намалява, ако вече изпреварват max(id)."""
        for table in (
            "users",
            "account_moderation_logs",
            "password_reset_tokens",
        ):
            seq = self._session.scalar(
                text(f"SELECT pg_get_serial_sequence('{table}', 'id')")
            )
            if not seq:
                continue

            max_id = self._session.scalar(text(f"SELECT MAX(id) FROM {table}"))
            if max_id is None:
                continue

            seq_row = (
                self._session.execute(text(f"SELECT last_value, is_called FROM {seq}"))
                .mappings()
                .one()
            )
            curr_val = int(seq_row["last_value"])
            is_called = bool(seq_row["is_called"])

            if max_id > curr_val or (max_id == curr_val and not is_called):
                self._session.execute(
                    text(f"SELECT setval('{seq}', :new_val, true)"),
                    {"new_val": max_id},
                )
