from typing import Annotated

from fastapi import Depends

from app.dependencies import DbSession
from app.modules.cart.repository import CartRepository
from app.modules.cart.service import CartService
from app.modules.catalog.dependencies import (
    get_product_repository,
    get_seller_status_repository,
)
from app.modules.catalog.repository import ProductRepository, SellerStatusRepository


def get_cart_repository(session: DbSession) -> CartRepository:
    """Фабрика за CartRepository с текущата сесия на заявката."""
    return CartRepository(session)


def get_cart_service(
    session: DbSession,
    cart_repository: Annotated[CartRepository, Depends(get_cart_repository)],
    product_repository: Annotated[ProductRepository, Depends(get_product_repository)],
    seller_status_repository: Annotated[
        SellerStatusRepository, Depends(get_seller_status_repository)
    ],
) -> CartService:
    """Фабрика за CartService, осигуряваща обща сесия за хранилищата."""
    return CartService(
        cart_repository=cart_repository,
        product_repository=product_repository,
        session=session,
        seller_status_repository=seller_status_repository,
    )


CartServiceDep = Annotated[CartService, Depends(get_cart_service)]
