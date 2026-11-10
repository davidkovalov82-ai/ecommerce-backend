"""HTTP маршрути на Payments сервиса (/internal/payments/*, /webhooks/stripe, /health)."""

from typing import Any

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from app.dependencies import PaymentServiceDep, require_internal_auth
from app.schemas import (
    InternalCancelPaymentRequest,
    InternalCancelPaymentResponse,
    InternalCheckoutSessionRequest,
    InternalCheckoutSessionResponse,
)
from app.service import (
    AttemptTerminalError,
    InvalidPaymentAmount,
    OrderAlreadyPaid,
    PaymentStateConflict,
    StripeServiceError,
)
from app.stripe_client import StripeNotConfigured

health_router = APIRouter(tags=["health"])
internal_router = APIRouter(
    prefix="/internal/payments",
    tags=["internal-payments"],
    dependencies=[Depends(require_internal_auth)],
)
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@health_router.get("/health")
def health() -> dict[str, str]:
    """Liveness проверка за Docker Compose и оркестрация."""
    return {"status": "ok"}


@internal_router.post(
    "/checkout-sessions",
    response_model=InternalCheckoutSessionResponse,
    status_code=status.HTTP_200_OK,
)
def create_checkout_session(
    payload: InternalCheckoutSessionRequest,
    payment_service: PaymentServiceDep,
) -> InternalCheckoutSessionResponse:
    """Вътрешна операция на Commerce за създаване или извличане на Stripe сесия."""
    try:
        return payment_service.create_checkout_session(payload)
    except InvalidPaymentAmount as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_ORDER_AMOUNT", "message": str(exc)},
        )
    except OrderAlreadyPaid as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ORDER_ALREADY_PAID", "message": str(exc)},
        )
    except AttemptTerminalError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ATTEMPT_TERMINAL", "message": str(exc)},
        )
    except PaymentStateConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ORDER_STATUS_CONFLICT",
                "message": str(exc),
                "current_status": exc.current_status,
            },
        )
    except StripeNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "STRIPE_NOT_CONFIGURED", "message": str(exc)},
        )
    except StripeServiceError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "STRIPE_ERROR",
                "message": "Failed to communicate with Stripe API",
            },
        )


@internal_router.post(
    "/orders/{order_id}/cancel",
    response_model=InternalCancelPaymentResponse,
    status_code=status.HTTP_200_OK,
)
def cancel_order_payment(
    order_id: int,
    payload: InternalCancelPaymentRequest,
    payment_service: PaymentServiceDep,
) -> InternalCancelPaymentResponse:
    """Вътрешна операция на Commerce за безопасна проверка и прекратяване на активна Stripe сесия."""
    try:
        return payment_service.cancel_or_expire_order(order_id, payload)
    except OrderAlreadyPaid as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ORDER_ALREADY_PAID", "message": str(exc)},
        )
    except PaymentStateConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ORDER_STATUS_CONFLICT",
                "message": str(exc),
                "current_status": exc.current_status,
            },
        )
    except StripeNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "STRIPE_NOT_CONFIGURED", "message": str(exc)},
        )
    except StripeServiceError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "STRIPE_ERROR",
                "message": "Failed to expire Stripe session",
            },
        )


@webhook_router.post("/stripe")
async def stripe_webhook(
    request: Request,
    payment_service: PaymentServiceDep,
) -> dict[str, Any]:
    """Публичен Stripe webhook endpoint; валидира криптографския подпис по суровите байтове."""
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")
    try:
        return await run_in_threadpool(
            payment_service.process_stripe_webhook, payload, sig_header
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_STRIPE_SIGNATURE", "message": str(exc)},
        )
    except StripeNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "STRIPE_NOT_CONFIGURED", "message": str(exc)},
        )
