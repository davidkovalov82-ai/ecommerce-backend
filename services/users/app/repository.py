"""Слой за достъп до данни (Repository) за потребителите."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User


class UserRepository:
    """Хранилище за изпълнение на SQL заявки към таблицата users."""

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
        """Заключва реда на потребителя с FOR UPDATE."""
        stmt = select(User).where(User.id == user_id).with_for_update()
        return self._session.scalar(stmt)
