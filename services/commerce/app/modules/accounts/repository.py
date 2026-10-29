from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.accounts.models import User


class UserRepository:
    """Хранилище за достъп до данни за потребители."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_email(self, email: str) -> User | None:
        """Търси потребител по нормализиран имейл адрес."""
        stmt = select(User).where(User.email == email)
        return self._session.scalars(stmt).first()

    def get_by_id(self, user_id: int) -> User | None:
        """Извлича потребител по ID."""
        return self._session.get(User, user_id)

    def create(self, user: User) -> User:
        """Добавя нов потребител в сесията без да изпълнява commit.

        Транзакцията и commit/rollback се управляват от Service слоя.
        """
        self._session.add(user)
        self._session.flush()
        return user
