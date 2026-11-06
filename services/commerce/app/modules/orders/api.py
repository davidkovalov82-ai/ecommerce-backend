import uuid
from typing import Annotated, Any

import stripe
from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from starlette.concurrency import run_in_threadpool

from app.modules.accounts.dependencies import CurrentUserDep
from app.modules.orders.dependencies import OrderServiceDep
from app.modules.orders.schemas import (
    CreateOrderRequest,
    OrderDetailResponse,
    OrderListResponse,
    OrderSummaryResponse,
    StripeCheckoutSessionResponse,
)
from app.modules.orders.service import (
    CartEmpty,
    CartProductUnavailable,
    InsufficientStock,
    InvalidOrderAmount,
    OrderAlreadyPaid,
    OrderNotFound,
    OrderStateConflict,
    StripeNotConfigured,
    StripeServiceError,
)

router = APIRouter(prefix="/orders", tags=["orders"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])


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
    "/{order_id}/checkout-session",
    response_model=StripeCheckoutSessionResponse,
    status_code=status.HTTP_200_OK,
)
def create_checkout_session(
    order_id: int,
    current_user: CurrentUserDep,
    order_service: OrderServiceDep,
) -> StripeCheckoutSessionResponse:
    """HTTP обработчик за създаване или извличане на отворена Stripe Checkout сесия."""
    try:
        checkout_url, session_id = order_service.create_stripe_checkout_session(
            order_id=order_id,
            user_id=current_user.id,
        )
        return StripeCheckoutSessionResponse(
            checkout_url=checkout_url,
            session_id=session_id,
        )
    except OrderNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"},
        )
    except OrderStateConflict as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ORDER_STATUS_CONFLICT",
                "message": f"Cannot create checkout session for order with status '{e.current_status}'",
            },
        )
    except InvalidOrderAmount as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_ORDER_AMOUNT", "message": str(e)},
        )
    except StripeNotConfigured as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "STRIPE_NOT_CONFIGURED", "message": str(e)},
        )
    except StripeServiceError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "STRIPE_ERROR",
                "message": "Failed to communicate with Stripe API",
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
    except StripeNotConfigured as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "STRIPE_NOT_CONFIGURED", "message": str(e)},
        )
    except StripeServiceError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "STRIPE_ERROR",
                "message": "Failed to communicate with Stripe API",
            },
        )


@webhook_router.post("/stripe")
async def stripe_webhook(
    request: Request,
    order_service: OrderServiceDep,
) -> dict[str, Any]:
    """HTTP обработчик за входящи Stripe webhook събития с валидация на криптографски подпис."""
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")
    try:
        return await run_in_threadpool(
            order_service.process_stripe_webhook, payload, sig_header
        )
    except (ValueError, stripe.SignatureVerificationError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_STRIPE_SIGNATURE", "message": str(e)},
        )
    except StripeNotConfigured as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "STRIPE_NOT_CONFIGURED", "message": str(e)},
        )
