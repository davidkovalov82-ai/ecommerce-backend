"""Управление на PostgreSQL връзката и сесиите за Payments."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Database:
    """Управлява жизнения цикъл на връзката със собствената база данни."""

    def __init__(self, database_url: str, **engine_kwargs: Any) -> None:
        self._engine: Engine = create_engine(database_url, **engine_kwargs)
        self._sessionmaker: sessionmaker[Session] = sessionmaker(
            bind=self._engine,
            autoflush=False,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> Engine:
        """Директен достъп до SQLAlchemy Engine."""
        return self._engine

    @property
    def sessionmaker(self) -> sessionmaker[Session]:
        """Фабрика за SQLAlchemy сесии."""
        return self._sessionmaker

    @contextmanager
    def session(self) -> Generator[Session]:
        """Контекстен мениджър за безопасно отваряне и затваряне на сесия."""
        session: Session = self._sessionmaker()
        try:
            yield session
        finally:
            session.close()

    def close(self) -> None:
        """Освобождава връзките в пула при спиране на процеса."""
        self._engine.dispose()


db = Database(settings.database_url, pool_pre_ping=True)
engine = db.engine
SessionLocal = db.sessionmaker


class Base(DeclarativeBase):
    """Базов клас за SQLAlchemy ORM моделите в Payments."""
