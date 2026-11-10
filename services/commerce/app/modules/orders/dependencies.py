from typing import Annotated

from fastapi import Depends

from app.dependencies import DbSession
from app.modules.accounts.dependencies import get_user_repository
from app.modules.accounts.repository import UserRepository
from app.modules.cart.dependencies import get_cart_repository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.dependencies import get_product_repository
from app.modules.catalog.repository import ProductRepository
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
    cart_repository: Annotated[CartRepository, Depends(get_cart_repository)],
    product_repository: Annotated[ProductRepository, Depends(get_product_repository)],
    user_repository: Annotated[UserRepository, Depends(get_user_repository)],
    payments_client: Annotated[PaymentsClient, Depends(get_payments_client)],
) -> OrderService:
    """Фабрика за OrderService, осигуряваща споделена сесия за всички участващи хранилища."""
    return OrderService(
        order_repository=order_repository,
        cart_repository=cart_repository,
        product_repository=product_repository,
        user_repository=user_repository,
        session=session,
        payments_client=payments_client,
    )


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
