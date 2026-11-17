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

oauth2_scheme = HTTPBearer(auto_error=False)


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
) -> TokenData:
    """Извлича и верифицира потребителя по Bearer токен (RS256 JWT от Users).

    Заявки с фалшиви заглавки X-User-Id/X-User-Role без валиден JWT връщат 401.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Authentication required",
            },
        )

    try:
        return decode_access_token(credentials.credentials)
    except InvalidToken as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": str(exc)},
        ) from exc


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
    if not settings.internal_auth_token:
        return
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
