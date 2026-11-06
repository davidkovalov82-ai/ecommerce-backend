from typing import Annotated

from fastapi import Depends

from app.dependencies import DbSession
from app.modules.accounts.dependencies import get_user_repository
from app.modules.accounts.repository import UserRepository
from app.modules.cart.dependencies import get_cart_repository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.dependencies import get_product_repository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from app.modules.orders.simulator import PaymentSimulator
from app.modules.orders.stripe_client import StripeClient


def get_stripe_client() -> StripeClient:
    """Фабрика за StripeClient."""
    return StripeClient()


def get_payment_simulator() -> PaymentSimulator:
    """Фабрика за локален симулатор на плащания."""
    return PaymentSimulator()


def get_order_repository(session: DbSession) -> OrderRepository:
    """Фабрика за OrderRepository с текущата сесия на заявката."""
    return OrderRepository(session)


def get_order_service(
    session: DbSession,
    order_repository: Annotated[OrderRepository, Depends(get_order_repository)],
    cart_repository: Annotated[CartRepository, Depends(get_cart_repository)],
    product_repository: Annotated[ProductRepository, Depends(get_product_repository)],
    user_repository: Annotated[UserRepository, Depends(get_user_repository)],
    payment_simulator: Annotated[PaymentSimulator, Depends(get_payment_simulator)],
    stripe_client: Annotated[StripeClient, Depends(get_stripe_client)],
) -> OrderService:
    """Фабрика за OrderService, осигуряваща споделена сесия за всички участващи хранилища."""
    return OrderService(
        order_repository=order_repository,
        cart_repository=cart_repository,
        product_repository=product_repository,
        user_repository=user_repository,
        session=session,
        payment_simulator=payment_simulator,
        stripe_client=stripe_client,
    )


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
