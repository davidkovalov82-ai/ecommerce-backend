"""FastAPI зависимости за Cart услугата."""

import hmac
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.catalog_client import CatalogClient
from app.config import settings
from app.db import SessionLocal
from app.repository import CartRepository
from app.service import CartService
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


def get_cart_repository(session: DbSession) -> CartRepository:
    """Връща CartRepository за сесията."""
    return CartRepository(session)


def get_catalog_client() -> Generator[CatalogClient]:
    """Предоставя CatalogClient за междусервизна комуникация."""
    client = CatalogClient()
    try:
        yield client
    finally:
        client.close()


CatalogClientDep = Annotated[CatalogClient, Depends(get_catalog_client)]


def get_cart_service(
    session: DbSession,
    cart_repo: Annotated[CartRepository, Depends(get_cart_repository)],
    catalog_client: CatalogClientDep,
) -> CartService:
    """Фабрика за CartService."""
    return CartService(
        session=session,
        cart_repository=cart_repo,
        catalog_client=catalog_client,
    )


CartServiceDep = Annotated[CartService, Depends(get_cart_service)]


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(oauth2_scheme)],
) -> TokenData:
    """Извлича и верифицира потребителя по Bearer токен (RS256 JWT от Users).

    Владелецът се определя строго от токена, никога от тялото или фалшиви заглавки.
    Заявки без токен връщат 401.
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


def verify_internal_auth(
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    """Проверява таен токен за междусервизни заявки от Orders/Commerce."""
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
