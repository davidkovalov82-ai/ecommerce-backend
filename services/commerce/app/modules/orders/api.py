import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, status

from app.modules.accounts.dependencies import CurrentUserDep
from app.modules.orders.dependencies import OrderServiceDep
from app.modules.orders.schemas import (
    CreateOrderRequest,
    OrderDetailResponse,
    OrderListResponse,
    OrderSummaryResponse,
)
from app.modules.orders.service import (
    CartEmpty,
    CartProductUnavailable,
    InsufficientStock,
    OrderNotFound,
)

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "",
    response_model=OrderDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_order(
    payload: CreateOrderRequest,
    current_user: CurrentUserDep,
    order_service: OrderServiceDep,
    idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> OrderDetailResponse:
    """HTTP обработчик за оформяне на цялата текуща количка в поръчка с идемпотентност."""
    try:
        order = order_service.create_order(
            user_id=current_user.id,
            idempotency_key=str(idempotency_key),
        )
        return OrderDetailResponse.model_validate(order)
    except CartEmpty:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CART_EMPTY", "message": "Shopping cart is empty"},
        )
    except CartProductUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CART_PRODUCT_UNAVAILABLE",
                "message": f"Product with id {e.product_id} is unavailable",
                "product_id": e.product_id,
            },
        )
    except InsufficientStock as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "INSUFFICIENT_STOCK",
                "message": f"Insufficient stock for product {e.product_id}",
                "product_id": e.product_id,
            },
        )


@router.get("", response_model=OrderListResponse)
def list_orders(
    current_user: CurrentUserDep,
    order_service: OrderServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> OrderListResponse:
    """HTTP обработчик за пагиниран списък със собствените поръчки на потребителя."""
    orders, total = order_service.list_orders(
        user_id=current_user.id,
        limit=limit,
        offset=offset,
    )
    return OrderListResponse(
        items=[OrderSummaryResponse.model_validate(o) for o in orders],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{order_id}", response_model=OrderDetailResponse)
def get_order(
    order_id: int,
    current_user: CurrentUserDep,
    order_service: OrderServiceDep,
) -> OrderDetailResponse:
    """HTTP обработчик за извличане на детайли за конкретна собствена поръчка."""
    try:
        order = order_service.get_order(
            order_id=order_id,
            user_id=current_user.id,
        )
        return OrderDetailResponse.model_validate(order)
    except OrderNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"},
        )
