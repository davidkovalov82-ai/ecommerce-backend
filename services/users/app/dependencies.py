"""FastAPI зависимости за инжектиране на сесии, хранилища и услуги в Users."""

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.repository import UserRepository
from app.service import UserService


def get_db_session() -> Generator[Session]:
    """Отваря и гарантирано затваря синхронна SQLAlchemy сесия за една HTTP заявка."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db_session)]


def get_user_repository(session: DbSession) -> UserRepository:
    """Връща инстанция на UserRepository за текущата сесия."""
    return UserRepository(session)


def get_user_service(
    session: DbSession,
    repository: Annotated[UserRepository, Depends(get_user_repository)],
) -> UserService:
    """Връща инстанция на UserService с текущата сесия и репозитори."""
    return UserService(
        user_repository=repository,
        session=session,
    )


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
