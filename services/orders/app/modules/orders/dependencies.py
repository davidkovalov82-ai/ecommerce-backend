from typing import Annotated

from fastapi import Depends

from app.dependencies import DbSession
from app.modules.accounts.dependencies import get_users_client
from app.modules.accounts.users_client import UsersClient
from app.modules.cart.client import CartClient
from app.modules.cart.dependencies import get_cart_client
from app.modules.catalog.client import CatalogClient
from app.modules.catalog.dependencies import get_catalog_client
from app.modules.orders.payments_client import HttpPaymentsClient, PaymentsClient
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService


def get_payments_client() -> PaymentsClient:
    """Фабрика за вътрешния HTTP клиент към Payments сервиса."""
    return HttpPaymentsClient()


def get_order_repository(session: DbSession) -> OrderRepository:
    """Фабрика за OrderRepository с текущата сесия на заявката."""
    return OrderRepository(session)


def get_order_service(
    session: DbSession,
    order_repository: Annotated[OrderRepository, Depends(get_order_repository)],
    cart_client: Annotated[CartClient, Depends(get_cart_client)],
    payments_client: Annotated[PaymentsClient, Depends(get_payments_client)],
    users_client: Annotated[UsersClient, Depends(get_users_client)],
    catalog_client: Annotated[CatalogClient, Depends(get_catalog_client)],
) -> OrderService:
    """Фабрика за OrderService."""
    return OrderService(
        order_repository=order_repository,
        cart_client=cart_client,
        session=session,
        payments_client=payments_client,
        users_client=users_client,
        catalog_client=catalog_client,
    )


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
