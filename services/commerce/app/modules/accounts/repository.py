from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.accounts.models import User


def get_by_email(session: Session, email: str) -> User | None:
    """Търси потребител по нормализиран имейл адрес."""
    stmt = select(User).where(User.email == email)
    return session.scalars(stmt).first()


def get_by_id(session: Session, user_id: int) -> User | None:
    """Извлича потребител по ID."""
    return session.get(User, user_id)


def create(session: Session, user: User) -> User:
    """Добавя нов потребител в сесията без да изпълнява commit.

    Транзакцията и commit/rollback се управляват от Service слоя.
    """
    session.add(user)
    session.flush()
    return user
