from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import SessionLocal


def get_db_session() -> Generator[Session]:
    """FastAPI зависимост за управление на жизнения цикъл на SQLAlchemy сесията.

    Създава нова сесия за всяка HTTP заявка и гарантира затварянето ѝ
    независимо от изхода на заявката (успех или грешка).
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# Type alias за удобна употреба в обработчиците
DbSession = Annotated[Session, Depends(get_db_session)]
