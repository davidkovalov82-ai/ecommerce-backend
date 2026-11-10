"""FastAPI зависимости и автентикация на вътрешните операции в Payments."""

import hmac
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.repository import PaymentRepository
from app.service import PaymentService
from app.stripe_client import StripeClient


def get_db_session() -> Generator[Session]:
    """Отваря и гарантирано затваря синхронна SQLAlchemy сесия за една HTTP заявка."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db_session)]


def get_stripe_client() -> StripeClient:
    """Фабрика за StripeClient (лесна за подмяна в тестове)."""
    return StripeClient()


def get_payment_repository(session: DbSession) -> PaymentRepository:
    return PaymentRepository(session)


def get_payment_service(
    session: DbSession,
    repository: Annotated[PaymentRepository, Depends(get_payment_repository)],
    stripe_client: Annotated[StripeClient, Depends(get_stripe_client)],
) -> PaymentService:
    return PaymentService(
        repository=repository,
        session=session,
        stripe_client=stripe_client,
    )


PaymentServiceDep = Annotated[PaymentService, Depends(get_payment_service)]


def require_internal_auth(
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    """Защитава вътрешните endpoints на Payments от произволни външни клиенти."""
    expected = settings.internal_auth_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INTERNAL_AUTH_NOT_CONFIGURED",
                "message": "Internal payment service token is not configured",
            },
        )
    if not x_internal_token or not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_INTERNAL_TOKEN",
                "message": "Unauthorized internal request",
            },
        )
