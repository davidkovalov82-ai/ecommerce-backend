"""FastAPI зависимости за Catalog."""

import hmac
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.repository import (
    CategoryRepository,
    ProductRepository,
    ReservationRepository,
    SellerStatusRepository,
)
from app.service import CatalogService
from app.tokens import InvalidToken, TokenData, decode_access_token
from app.users_client import (
    UsersClient,
    UsersServiceAuthError,
    UsersServiceUnavailableError,
)

oauth2_scheme = HTTPBearer(auto_error=False)
_users_client = UsersClient()


def get_users_client() -> UsersClient:
    """Връща клиент към Users сервиса."""
    return _users_client


def get_db_session() -> Generator[Session]:
    """Отваря и гарантирано затваря синхронна SQLAlchemy сесия за HTTP заявка."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db_session)]


def get_product_repository(session: DbSession) -> ProductRepository:
    """Връща ProductRepository."""
    return ProductRepository(session)


def get_category_repository(session: DbSession) -> CategoryRepository:
    """Връща CategoryRepository."""
    return CategoryRepository(session)


def get_seller_status_repository(session: DbSession) -> SellerStatusRepository:
    """Връща SellerStatusRepository."""
    return SellerStatusRepository(session)


def get_reservation_repository(session: DbSession) -> ReservationRepository:
    """Връща ReservationRepository."""
    return ReservationRepository(session)


def get_catalog_service(
    session: DbSession,
    product_repo: Annotated[ProductRepository, Depends(get_product_repository)],
    category_repo: Annotated[CategoryRepository, Depends(get_category_repository)],
    seller_status_repo: Annotated[
        SellerStatusRepository, Depends(get_seller_status_repository)
    ],
    reservation_repo: Annotated[
        ReservationRepository, Depends(get_reservation_repository)
    ],
) -> CatalogService:
    """Фабрика за CatalogService."""
    return CatalogService(
        session=session,
        product_repository=product_repo,
        category_repository=category_repo,
        seller_status_repository=seller_status_repo,
        reservation_repository=reservation_repo,
    )


CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(oauth2_scheme)],
    users_client: Annotated[UsersClient, Depends(get_users_client)],
) -> TokenData:
    """Извлича и верифицира потребителя по Bearer токен (RS256 JWT) и проверява статус в Users."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Authentication required",
            },
        )

    try:
        token_data = decode_access_token(credentials.credentials)
    except InvalidToken as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": str(exc)},
        ) from exc

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
        )

    if remote_user.token_version != token_data.token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_TOKEN",
                "message": "Token has been revoked",
            },
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

    return TokenData(
        user_id=remote_user.id,
        role=remote_user.role,
        email=remote_user.email,
        token_version=remote_user.token_version,
    )


CurrentUserDep = Annotated[TokenData, Depends(get_current_user)]


def get_admin_user(current_user: CurrentUserDep) -> TokenData:
    """Проверява за администраторски права."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "Admin privileges required"},
        )
    return current_user


AdminUserDep = Annotated[TokenData, Depends(get_admin_user)]


def verify_internal_auth(
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    """Проверява таен токен за междусервизни заявки от Commerce."""
    if not settings.internal_auth_token or not settings.internal_auth_token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INTERNAL_AUTH_REQUIRED",
                "message": "Internal authentication token is not configured",
            },
        )
    if not x_internal_token or not hmac.compare_digest(
        x_internal_token, settings.internal_auth_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INTERNAL_AUTH_REQUIRED",
                "message": "Valid X-Internal-Token header is required",
            },
        )
