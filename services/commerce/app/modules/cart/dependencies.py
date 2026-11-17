from typing import Annotated

from fastapi import Depends

from app.modules.cart.client import CartClient


def get_cart_client() -> CartClient:
    """Фабрика за CartClient."""
    return CartClient()


def resolve_cart_client(explicit: CartClient | None = None) -> CartClient:
    """Връща подадения клиент, текущия override от app.dependency_overrides или нов CartClient."""
    if explicit is not None:
        return explicit
    from app.main import app

    override = app.dependency_overrides.get(get_cart_client)
    if override is not None:
        return override()
    return CartClient()


CartClientDep = Annotated[CartClient, Depends(get_cart_client)]
