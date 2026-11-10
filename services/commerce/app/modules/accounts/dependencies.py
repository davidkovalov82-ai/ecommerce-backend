"""FastAPI зависимости за автентикация, права и клиенти в Commerce."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash

from app.config import settings
from app.dependencies import DbSession
from app.modules.accounts.repository import UserRepository
from app.modules.accounts.service import AccountService
from app.modules.accounts.tokens import InvalidToken, decode_access_token
from app.modules.accounts.users_client import (
    UsersClient,
    UsersServiceAuthError,
    UsersServiceUnavailableError,
)

# Схема за Bearer автентикация в OpenAPI / Swagger UI
oauth2_scheme = HTTPBearer(auto_error=False)

# Инстанция на PasswordHash с препоръчаните настройки за Argon2
_password_hasher = PasswordHash.recommended()


@dataclass
class AuthenticatedUser:
    """Модел за автентикиран потребител в Commerce, верифициран от Users."""

    id: int
    email: str
    role: str
    status: str
    token_version: int = 0
    display_name: str | None = None


def get_password_hasher() -> PasswordHash:
    """Връща инстанция на PasswordHash с препоръчаните Argon2 настройки."""
    return _password_hasher


def get_user_repository(session: DbSession) -> UserRepository:
    """Фабрика за UserRepository с текущата сесия на заявката."""
    return UserRepository(session)


def get_account_service(
    session: DbSession,
    user_repository: Annotated[UserRepository, Depends(get_user_repository)],
    password_hasher: Annotated[PasswordHash, Depends(get_password_hasher)],
) -> AccountService:
    """Фабрика за AccountService, осигуряваща обща сесия с UserRepository."""
    return AccountService(
        user_repository=user_repository,
        session=session,
        password_hasher=password_hasher,
    )


AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]

_users_client = UsersClient()


def get_users_client() -> UsersClient:
    """Фабрика за HTTP клиент към микросервиса Users."""
    return _users_client


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(oauth2_scheme)],
    users_client: Annotated[UsersClient, Depends(get_users_client)],
) -> AuthenticatedUser:
    """Зависимост за верифициране на JWT токен по RS256 публичен ключ и проверка на статус в Users."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Authentication required",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    public_key = settings.get_public_key()
    try:
        token_data = decode_access_token(
            token=credentials.credentials,
            public_key=public_key,
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

    try:
        remote_user = users_client.get_user_status(token_data.user_id)
    except (UsersServiceUnavailableError, UsersServiceAuthError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "USERS_SERVICE_UNAVAILABLE",
                "message": "Users service is unavailable",
            },
        )

    if remote_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "USER_NOT_FOUND",
                "message": "User not found",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if remote_user.token_version != token_data.token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_TOKEN",
                "message": "Token has been revoked",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if remote_user.status == "blocked":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "ACCOUNT_BLOCKED",
                "message": "Account is blocked",
            },
        )
    if remote_user.status == "closed":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "ACCOUNT_CLOSED",
                "message": "Account is closed",
            },
        )
    if remote_user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "ACCOUNT_INACTIVE",
                "message": "Account is inactive",
            },
        )

    return AuthenticatedUser(
        id=remote_user.id,
        email=remote_user.email,
        role=remote_user.role,
        status=remote_user.status,
        token_version=remote_user.token_version,
        display_name=remote_user.display_name,
    )


CurrentUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]


def require_admin(
    current_user: CurrentUserDep,
) -> AuthenticatedUser:
    """Зависимост, проверяваща дали текущият потребител притежава администраторска роля (admin)."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "Admin privileges required",
            },
        )
    return current_user


AdminUserDep = Annotated[AuthenticatedUser, Depends(require_admin)]
