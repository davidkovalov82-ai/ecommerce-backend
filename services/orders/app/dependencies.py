from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import db


def get_db_session() -> Generator[Session]:
    """FastAPI зависимост за управление на жизнения цикъл на SQLAlchemy сесията.

    Създава нова сесия за всяка HTTP заявка и гарантира затварянето ѝ
    независимо от изхода на заявката (успех или грешка).
    """
    with db.session() as session:
        yield session


# Type alias за удобна употреба в обработчиците
DbSession = Annotated[Session, Depends(get_db_session)]
