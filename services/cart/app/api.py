"""HTTP обработчици за Cart (публични и вътрешни)."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import (
    CartServiceDep,
    CurrentUserDep,
    verify_internal_auth,
)
from app.schemas import (
    CartItemPayload,
    CartResponse,
    ClearPurchasedRequest,
    ClearPurchasedResponse,
)
from app.service import InsufficientStock, ProductNotFound

cart_router = APIRouter(prefix="/cart", tags=["cart"])
internal_router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(verify_internal_auth)],
)
health_router = APIRouter(tags=["health"])


@health_router.get("/health")
def health() -> dict[str, str]:
    """Проверка за здраве и работоспособност на Cart услугата."""
    return {"status": "ok"}


@cart_router.get("", response_model=CartResponse)
def get_cart(
    current_user: CurrentUserDep,
    cart_service: CartServiceDep,
) -> CartResponse:
    """Извлича състоянието на текущата потребителска количка."""
    return cart_service.get_cart(user_id=current_user.user_id)


@cart_router.put("/items/{product_id}", response_model=CartResponse)
def update_cart_item(
    product_id: int,
    payload: CartItemPayload,
    current_user: CurrentUserDep,
    cart_service: CartServiceDep,
) -> CartResponse:
    """Задава точно количество за продукт в количката. Не резервира остатък."""
    try:
        return cart_service.set_item_quantity(
            user_id=current_user.user_id,
            product_id=product_id,
            quantity=payload.quantity,
        )
    except ProductNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )
    except InsufficientStock:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "INSUFFICIENT_STOCK",
                "message": "Insufficient stock for requested quantity",
            },
        )


@cart_router.delete("/items/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_cart_item(
    product_id: int,
    current_user: CurrentUserDep,
    cart_service: CartServiceDep,
) -> None:
    """Премахва продукт от потребителската количка."""
    cart_service.remove_item(
        user_id=current_user.user_id,
        product_id=product_id,
    )


@internal_router.get("/cart/{user_id}", response_model=CartResponse)
def get_internal_cart(
    user_id: int,
    cart_service: CartServiceDep,
) -> CartResponse:
    """Вътрешен API за Orders: извличане на моментна снимка на количката."""
    return cart_service.get_cart(user_id=user_id)


@internal_router.post(
    "/cart/{user_id}/clear-purchased",
    response_model=ClearPurchasedResponse,
    status_code=status.HTTP_200_OK,
)
def clear_purchased_items(
    user_id: int,
    payload: ClearPurchasedRequest,
    cart_service: CartServiceDep,
) -> ClearPurchasedResponse:
    """Вътрешен API за Orders: изчистване само на закупените позиции след поръчка."""
    versioned_items = [(it.product_id, it.version) for it in payload.items]

    cleared = cart_service.clear_purchased(
        user_id=user_id,
        order_id=payload.order_id,
        items=versioned_items,
    )
    return ClearPurchasedResponse(
        status="cleared",
        user_id=user_id,
        cleared_product_ids=cleared,
        order_id=payload.order_id,
    )
