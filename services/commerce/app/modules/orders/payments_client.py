"""Вътрешен HTTP клиент на Commerce за комуникация с независимия Payments сервис."""

import datetime
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from app.config import settings


class PaymentGatewayError(Exception):
    """Грешка или таймаут при комуникация с Payments или външния Stripe."""


class PaymentsNotConfigured(Exception):
    """Липсва конфигурация за вътрешна автентикация или Stripe в Payments."""


class RemoteOrderAlreadyPaid(Exception):
    """Payments отчете, че поръчката вече има потвърдено успешно плащане."""


class RemoteOrderAmountInvalid(Exception):
    """Payments отхвърли сумата на поръчката като невалидна за Stripe."""


class RemotePaymentConflict(Exception):
    """Конфликт с текущото състояние на плащането в Payments."""

    def __init__(self, current_status: str, message: str) -> None:
        self.current_status = current_status
        super().__init__(message)


@dataclass(frozen=True)
class CheckoutSessionResult:
    """Отговор от Payments при създаване/извличане на Checkout Session."""

    payment_attempt_id: int
    session_id: str
    checkout_url: str
    status: str


@dataclass(frozen=True)
class CancelPaymentResult:
    """Отговор от Payments при проверка и прекратяване на активна сесия."""

    status: Literal["can_cancel", "already_expired"]
    action: str


class PaymentsClient(Protocol):
    """Протокол за вътрешния клиент към Payments сервиса."""

    def create_checkout_session(
        self,
        *,
        order_id: int,
        attempt_key: str,
        amount_minor: int,
        currency: str,
        line_items: list[dict[str, Any]],
        order_expires_at: datetime.datetime | None,
    ) -> CheckoutSessionResult: ...

    def cancel_order_payment(
        self,
        *,
        order_id: int,
        reason: Literal["cancel", "reserve_expired"] = "cancel",
        order_expires_at: datetime.datetime | None = None,
    ) -> CancelPaymentResult: ...


def _extract_error_detail(resp: httpx.Response) -> tuple[str | None, str | None, str]:
    try:
        body = resp.json()
    except ValueError:
        return None, None, resp.text[:300]
    detail = body.get("detail")
    if isinstance(detail, dict):
        code = detail.get("code")
        cur_status = detail.get("current_status")
        message = str(detail.get("message") or "")
        return (
            str(code) if code is not None else None,
            str(cur_status) if cur_status is not None else None,
            message,
        )
    return None, None, str(detail or "")


class HttpPaymentsClient:
    """Синхронен httpx клиент за защитеното вътрешно HTTP API на Payments."""

    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.payments_base_url).rstrip("/")
        self.internal_token = (
            internal_token
            if internal_token is not None
            else settings.payments_internal_auth_token
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.payments_timeout_seconds
        )

    def _headers(self) -> dict[str, str]:
        if not self.internal_token:
            raise PaymentsNotConfigured(
                "PAYMENTS_INTERNAL_AUTH_TOKEN не е конфигуриран"
            )
        return {"X-Internal-Token": self.internal_token}

    def create_checkout_session(
        self,
        *,
        order_id: int,
        attempt_key: str,
        amount_minor: int,
        currency: str,
        line_items: list[dict[str, Any]],
        order_expires_at: datetime.datetime | None,
    ) -> CheckoutSessionResult:
        payload = {
            "order_id": order_id,
            "attempt_key": attempt_key,
            "amount_minor": amount_minor,
            "currency": currency,
            "line_items": line_items,
            "order_expires_at": (
                order_expires_at.isoformat() if order_expires_at is not None else None
            ),
        }
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=self.timeout_seconds
            ) as client:
                resp = client.post(
                    "/internal/payments/checkout-sessions",
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise PaymentGatewayError(
                f"Failed to reach Payments service: {type(exc).__name__}"
            ) from exc

        if resp.status_code == 200:
            data = resp.json()
            return CheckoutSessionResult(
                payment_attempt_id=int(data["payment_attempt_id"]),
                session_id=str(data["session_id"]),
                checkout_url=str(data["checkout_url"]),
                status=str(data["status"]),
            )

        self._raise_mapped_error(resp)
        raise AssertionError("Unreachable")

    def cancel_order_payment(
        self,
        *,
        order_id: int,
        reason: Literal["cancel", "reserve_expired"] = "cancel",
        order_expires_at: datetime.datetime | None = None,
    ) -> CancelPaymentResult:
        payload = {
            "reason": reason,
            "order_expires_at": (
                order_expires_at.isoformat() if order_expires_at is not None else None
            ),
        }
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=self.timeout_seconds
            ) as client:
                resp = client.post(
                    f"/internal/payments/orders/{order_id}/cancel",
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise PaymentGatewayError(
                f"Failed to reach Payments service: {type(exc).__name__}"
            ) from exc

        if resp.status_code == 200:
            data = resp.json()
            return CancelPaymentResult(
                status=data["status"],
                action=str(data.get("action", "")),
            )

        self._raise_mapped_error(resp)
        raise AssertionError("Unreachable")

    @staticmethod
    def _raise_mapped_error(resp: httpx.Response) -> None:
        code, current_status, message = _extract_error_detail(resp)
        if resp.status_code == 400 and code == "INVALID_ORDER_AMOUNT":
            raise RemoteOrderAmountInvalid(message or "Invalid order amount")
        if resp.status_code == 409:
            if code == "ORDER_ALREADY_PAID":
                raise RemoteOrderAlreadyPaid(message or "Order is already paid")
            raise RemotePaymentConflict(
                current_status=current_status or code or "conflict",
                message=message or "Payment state conflict",
            )
        if resp.status_code == 503:
            raise PaymentsNotConfigured(message or "Payments service not configured")
        raise PaymentGatewayError(
            f"Payments service error status={resp.status_code} code={code}"
        )
