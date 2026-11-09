from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash

from app.config import settings
from app.dependencies import DbSession
from app.modules.accounts.models import User
from app.modules.accounts.repository import UserRepository
from app.modules.accounts.service import AccountService
from app.modules.accounts.tokens import InvalidToken, decode_access_token

# Схема за Bearer автентикация в OpenAPI / Swagger UI
oauth2_scheme = HTTPBearer(auto_error=False)

# Инстанция на PasswordHash с препоръчаните настройки за Argon2
_password_hasher = PasswordHash.recommended()


def get_password_hasher() -> PasswordHash:
    """Връща инстанция на PasswordHash с препоръчаните Argon2 настройки."""
    return _password_hasher


# какво това е не разбирам фабрика за какво исползваме фабрика каква е идея?
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


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(oauth2_scheme)],
    user_repository: Annotated[UserRepository, Depends(get_user_repository)],
) -> User:
    """Зависимост за извличане и верифициране на текущия автентикиран потребител по JWT токен."""
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
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
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
    """Зависимост, проверяваща дали текущият автентикиран потребител притежава администраторска роля (admin)."""
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
