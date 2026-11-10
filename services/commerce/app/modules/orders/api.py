import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, status

from app.modules.accounts.dependencies import CurrentUserDep
from app.modules.orders.dependencies import OrderServiceDep
from app.modules.orders.payments_client import (
    PaymentGatewayError,
    PaymentsNotConfigured,
)
from app.modules.orders.schemas import (
    CreateOrderRequest,
    OrderDetailResponse,
    OrderListResponse,
    OrderSummaryResponse,
    SellerShipmentDetailResponse,
    SellerShipmentListResponse,
    SellerShipmentSummaryResponse,
    StripeCheckoutSessionResponse,
)
from app.modules.orders.service import (
    CartEmpty,
    CartProductUnavailable,
    IdempotencyPayloadConflict,
    InsufficientStock,
    InvalidOrderAmount,
    MissingShippingData,
    OrderAlreadyPaid,
    OrderNotFound,
    OrderNotPaid,
    OrderStateConflict,
    ShipmentNotFound,
)

router = APIRouter(prefix="/orders", tags=["orders"])
seller_shipments_router = APIRouter(
    prefix="/seller/shipments", tags=["seller-shipments"]
)


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
            shipping_address=payload.shipping_address,
            customer_email=current_user.email,
        )
        return OrderDetailResponse.model_validate(order)
    except IdempotencyPayloadConflict as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH",
                "message": str(e),
            },
        )
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
    except PaymentsNotConfigured as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "PAYMENTS_NOT_CONFIGURED", "message": str(e)},
        )
    except PaymentGatewayError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "PAYMENT_GATEWAY_ERROR",
                "message": "Failed to communicate with Payments service",
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
    except PaymentsNotConfigured as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "PAYMENTS_NOT_CONFIGURED", "message": str(e)},
        )
    except PaymentGatewayError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "PAYMENT_GATEWAY_ERROR",
                "message": "Failed to communicate with Payments service",
            },
        )


@seller_shipments_router.get("", response_model=SellerShipmentListResponse)
def list_seller_shipments(
    current_user: CurrentUserDep,
    order_service: OrderServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SellerShipmentListResponse:
    """HTTP обработчик за пагиниран списък със собствените пратки на продавача."""
    shipments, total = order_service.list_seller_shipments(
        seller_id=current_user.id,
        limit=limit,
        offset=offset,
    )
    return SellerShipmentListResponse(
        items=[SellerShipmentSummaryResponse.model_validate(sh) for sh in shipments],
        total=total,
        limit=limit,
        offset=offset,
    )


@seller_shipments_router.get(
    "/{shipment_id}", response_model=SellerShipmentDetailResponse
)
def get_seller_shipment(
    shipment_id: int,
    current_user: CurrentUserDep,
    order_service: OrderServiceDep,
) -> SellerShipmentDetailResponse:
    """HTTP обработчик за детайли на собствена пратка на продавача (позиции + адрес)."""
    try:
        shipment = order_service.get_seller_shipment(
            shipment_id=shipment_id,
            seller_id=current_user.id,
        )
        return SellerShipmentDetailResponse.model_validate(shipment)
    except ShipmentNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SHIPMENT_NOT_FOUND", "message": "Shipment not found"},
        )


@seller_shipments_router.post(
    "/{shipment_id}/ship", response_model=SellerShipmentDetailResponse
)
def ship_seller_shipment(
    shipment_id: int,
    current_user: CurrentUserDep,
    order_service: OrderServiceDep,
) -> SellerShipmentDetailResponse:
    """HTTP обработчик за отбелязване на собствена пратка като изпратена от продавача."""
    try:
        shipment = order_service.ship_seller_shipment(
            shipment_id=shipment_id,
            seller_id=current_user.id,
        )
        return SellerShipmentDetailResponse.model_validate(shipment)
    except ShipmentNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SHIPMENT_NOT_FOUND", "message": "Shipment not found"},
        )
    except OrderNotPaid as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ORDER_NOT_PAID", "message": str(e)},
        )
    except MissingShippingData as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "MISSING_SHIPPING_DATA", "message": str(e)},
        )
