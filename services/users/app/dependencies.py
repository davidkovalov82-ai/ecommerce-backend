"""FastAPI зависимости за сесии, хранилища, автентикация и права в Users."""

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.mailer import AccountMailer
from app.models import User
from app.repository import UserRepository
from app.service import UserService
from app.tokens import InvalidToken, decode_access_token

oauth2_scheme = HTTPBearer(auto_error=False)


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


def get_account_mailer() -> AccountMailer:
    """Фабрика за AccountMailer."""
    return AccountMailer()


def get_user_service(
    session: DbSession,
    repository: Annotated[UserRepository, Depends(get_user_repository)],
    mailer: Annotated[AccountMailer, Depends(get_account_mailer)],
) -> UserService:
    """Връща инстанция на UserService с текущата сесия, репозитори и мейлър."""
    return UserService(
        user_repository=repository,
        session=session,
        mailer=mailer,
    )


UserServiceDep = Annotated[UserService, Depends(get_user_service)]


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(oauth2_scheme)],
    user_repository: Annotated[UserRepository, Depends(get_user_repository)],
) -> User:
    """Извлича и верифицира текущия автентикиран потребител по RS256 JWT токен от БД на Users."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Authentication required",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        token_data = decode_access_token(
            credentials.credentials,
            public_key=settings.get_public_key(),
            algorithm=settings.jwt_algorithm,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
    except InvalidToken:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_TOKEN",
                "message": "Invalid or expired token",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = user_repository.get_by_id(token_data.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "USER_NOT_FOUND",
                "message": "User not found",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.token_version != token_data.token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_TOKEN",
                "message": "Invalid or expired token",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "ACCOUNT_INACTIVE",
                "message": "Account is blocked or closed",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def require_admin(
    current_user: CurrentUserDep,
) -> User:
    """Проверява дали текущият потребител притежава актуална администраторска роля (admin) в БД."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "Admin privileges required",
            },
        )
    return current_user


AdminUserDep = Annotated[User, Depends(require_admin)]
