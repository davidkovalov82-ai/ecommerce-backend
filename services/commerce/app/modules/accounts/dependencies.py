from typing import Annotated

from fastapi import Depends
from pwdlib import PasswordHash

from app.dependencies import DbSession
from app.modules.accounts.repository import UserRepository
from app.modules.accounts.service import AccountService

# Инстанция на PasswordHash с препоръчаните настройки за Argon2
_password_hasher = PasswordHash.recommended()


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
