from fastapi import APIRouter, HTTPException, status

from app.modules.accounts.dependencies import CurrentUserDep
from app.modules.cart.dependencies import CartServiceDep
from app.modules.cart.schemas import CartItemUpdateRequest, CartResponse
from app.modules.cart.service import InsufficientStock
from app.modules.catalog.service import ProductNotFound

router = APIRouter(prefix="/cart", tags=["cart"])


@router.get("", response_model=CartResponse)
def get_cart(
    current_user: CurrentUserDep,
    cart_service: CartServiceDep,
) -> CartResponse:
    """HTTP обработчик за получаване на текущата количка на автентикирания потребител."""
    return cart_service.get_cart(user_id=current_user.id)


@router.put("/items/{product_id}", response_model=CartResponse)
def update_cart_item(
    product_id: int,
    data: CartItemUpdateRequest,
    current_user: CurrentUserDep,
    cart_service: CartServiceDep,
) -> CartResponse:
    """HTTP обработчик за задаване на точно количество на продукт в количката."""
    try:
        return cart_service.set_item_quantity(
            user_id=current_user.id,
            product_id=product_id,
            quantity=data.quantity,
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


@router.delete("/items/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_cart_item(
    product_id: int,
    current_user: CurrentUserDep,
    cart_service: CartServiceDep,
) -> None:
    """HTTP обработчик за премахване на продукт от количката на потребителя."""
    cart_service.remove_item(
        user_id=current_user.id,
        product_id=product_id,
    )
