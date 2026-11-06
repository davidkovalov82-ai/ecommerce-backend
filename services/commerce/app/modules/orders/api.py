import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Response, status

from app.modules.accounts.dependencies import CurrentUserDep
from app.modules.orders.dependencies import OrderServiceDep
from app.modules.orders.schemas import (
    CreateOrderRequest,
    CreatePaymentRequest,
    OrderDetailResponse,
    OrderListResponse,
    OrderSummaryResponse,
    PaymentAttemptResponse,
)
from app.modules.orders.service import (
    CartEmpty,
    CartProductUnavailable,
    IdempotencyConflict,
    InsufficientStock,
    OrderAlreadyPaid,
    OrderNotFound,
    OrderStateConflict,
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


@router.post(
    "/{order_id}/payments",
    response_model=PaymentAttemptResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_payment(
    order_id: int,
    payload: CreatePaymentRequest,
    current_user: CurrentUserDep,
    order_service: OrderServiceDep,
    idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
    response: Response,
) -> PaymentAttemptResponse:
    """HTTP обработчик за симулирано плащане на поръчка с идемпотентност."""
    try:
        attempt, is_new = order_service.create_payment(
            order_id=order_id,
            user_id=current_user.id,
            idempotency_key=str(idempotency_key),
            payment_method=payload.payment_method,
        )
        if not is_new:
            response.status_code = status.HTTP_200_OK
        return PaymentAttemptResponse.model_validate(attempt)
    except OrderNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"},
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "Idempotency key already used with different payment method",
            },
        )
    except OrderStateConflict as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ORDER_STATUS_CONFLICT",
                "message": f"Cannot pay order with status '{e.current_status}'",
            },
        )


@router.post(
    "/{order_id}/cancel",
    response_model=OrderDetailResponse,
    status_code=status.HTTP_200_OK,
)
def cancel_order(
    order_id: int,
    current_user: CurrentUserDep,
    order_service: OrderServiceDep,
) -> OrderDetailResponse:
    """HTTP обработчик за отказ от собствена поръчка и възстановяване на запазените наличности."""
    try:
        order = order_service.cancel_order(
            order_id=order_id,
            user_id=current_user.id,
        )
        return OrderDetailResponse.model_validate(order)
    except OrderNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"},
        )
    except OrderAlreadyPaid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ORDER_ALREADY_PAID",
                "message": "Cannot cancel paid order",
            },
        )
    except OrderStateConflict as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ORDER_STATUS_CONFLICT",
                "message": f"Cannot cancel order with status '{e.current_status}'",
            },
        )


