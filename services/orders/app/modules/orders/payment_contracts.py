"""Строги контракти на входящите платежни събития от Payments към Commerce."""

import datetime
import json
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
)

SUPPORTED_EVENT_VERSION = 1
PG_INT_MAX = 2**31 - 1
PG_BIGINT_MAX = 2**63 - 1


class PaymentContractError(Exception):
    """Базова грешка за невалидно платежно събитие -> DLQ."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class InvalidPaymentContract(PaymentContractError):
    """Нарушена структура, невалиден JSON или извън БД границите."""


class UnsupportedPaymentEventVersion(PaymentContractError):
    """Неподдържана версия на платежното събитие."""


class UnsupportedPaymentEventType(PaymentContractError):
    """Неподдържан тип платежно събитие."""


def _safe_error_field(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "validation_error"
    loc = errors[0].get("loc", ())
    parts = [str(p) for p in loc if isinstance(p, str) and p.replace("_", "").isalnum()]
    return ".".join(parts) if parts else "validation_error"


def _reject_json_constant(_token: str) -> Any:
    raise ValueError("invalid_json_constant")


class PaymentEventData(BaseModel):
    model_config = ConfigDict(extra="allow")

    order_id: Annotated[int, StrictInt()] = Field(gt=0, le=PG_BIGINT_MAX)
    payment_attempt_id: Annotated[int, StrictInt()] = Field(gt=0, le=PG_BIGINT_MAX)
    attempt_key: str = Field(min_length=1, max_length=100)
    stripe_session_id: str | None = Field(default=None, max_length=255)
    stripe_event_id: str | None = Field(default=None, max_length=255)
    amount_minor: Annotated[int, StrictInt()] = Field(ge=0, le=PG_BIGINT_MAX)
    currency: str = Field(min_length=3, max_length=3)
    mode: Literal["test", "live"] = "test"


class OrderClosureEventData(BaseModel):
    """Данни за поръчково закритие без обвързана платежна попытка (напр. отмяна преди checkout)."""

    model_config = ConfigDict(extra="allow")

    order_id: Annotated[int, StrictInt()] = Field(gt=0, le=PG_BIGINT_MAX)
    payment_attempt_id: None = None
    attempt_key: str | None = Field(default=None, min_length=1, max_length=100)
    stripe_session_id: str | None = Field(default=None, max_length=255)
    stripe_event_id: str | None = Field(default=None, max_length=255)
    closure_reason: str | None = Field(default=None, max_length=50)


class PaymentEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str = Field(min_length=1, max_length=36)
    event_type: str = Field(min_length=1, max_length=100)
    event_version: Annotated[int, StrictInt()] = Field(ge=1, le=PG_INT_MAX)
    occurred_at: datetime.datetime
    producer: str = Field(min_length=1, max_length=50)
    data: dict[str, Any]

    @field_validator("event_id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        return str(uuid.UUID(v))


@dataclass(frozen=True)
class ValidatedPaymentEvent:
    event_id: str
    event_type: Literal[
        "payment.succeeded",
        "payment.processing",
        "payment.failed",
        "payment.expired",
        "payment.cancelled",
    ]
    event_version: int
    occurred_at: datetime.datetime
    order_id: int
    payment_attempt_id: int | None
    attempt_key: str | None
    stripe_session_id: str | None
    stripe_event_id: str | None
    amount_minor: int | None
    currency: str | None
    mode: Literal["test", "live"] | None
    payload: dict[str, Any]


def parse_and_validate_payment_event(raw_body: bytes) -> ValidatedPaymentEvent:
    """Разчита суровите байтове от RabbitMQ и валидира платежния контракт без теч на данни."""
    try:
        raw_obj = json.loads(
            raw_body.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InvalidPaymentContract("invalid_json") from exc

    if not isinstance(raw_obj, dict):
        raise InvalidPaymentContract("envelope_must_be_object")

    raw_version = raw_obj.get("event_version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise InvalidPaymentContract("invalid_event_version_type")
    if raw_version != SUPPORTED_EVENT_VERSION:
        raise UnsupportedPaymentEventVersion("unsupported_event_version")

    try:
        envelope = PaymentEventEnvelope.model_validate(raw_obj)
    except ValidationError as exc:
        raise InvalidPaymentContract(
            f"invalid_envelope:{_safe_error_field(exc)}"
        ) from exc

    if envelope.event_type not in (
        "payment.succeeded",
        "payment.processing",
        "payment.failed",
        "payment.expired",
        "payment.cancelled",
    ):
        raise UnsupportedPaymentEventType("unsupported_event_type")

    is_attemptless_cancellation = (
        envelope.event_type == "payment.cancelled"
        and envelope.data.get("payment_attempt_id") is None
    )
    if is_attemptless_cancellation:
        try:
            closure_data = OrderClosureEventData.model_validate(envelope.data)
        except ValidationError as exc:
            raise InvalidPaymentContract(
                f"invalid_payment_payload:{_safe_error_field(exc)}"
            ) from exc

        normalized_closure_payload = {
            "order_id": closure_data.order_id,
            "payment_attempt_id": None,
            "attempt_key": closure_data.attempt_key,
            "stripe_session_id": closure_data.stripe_session_id,
            "stripe_event_id": closure_data.stripe_event_id,
            "closure_reason": closure_data.closure_reason,
        }
        return ValidatedPaymentEvent(
            event_id=envelope.event_id,
            event_type="payment.cancelled",
            event_version=envelope.event_version,
            occurred_at=envelope.occurred_at,
            order_id=closure_data.order_id,
            payment_attempt_id=None,
            attempt_key=closure_data.attempt_key,
            stripe_session_id=closure_data.stripe_session_id,
            stripe_event_id=closure_data.stripe_event_id,
            amount_minor=None,
            currency=None,
            mode=None,
            payload=normalized_closure_payload,
        )

    try:
        data = PaymentEventData.model_validate(envelope.data)
    except ValidationError as exc:
        raise InvalidPaymentContract(
            f"invalid_payment_payload:{_safe_error_field(exc)}"
        ) from exc

    normalized_payload = {
        "order_id": data.order_id,
        "payment_attempt_id": data.payment_attempt_id,
        "attempt_key": data.attempt_key,
        "stripe_session_id": data.stripe_session_id,
        "stripe_event_id": data.stripe_event_id,
        "amount_minor": data.amount_minor,
        "currency": data.currency.upper(),
        "mode": data.mode,
    }
    return ValidatedPaymentEvent(
        event_id=envelope.event_id,
        event_type=envelope.event_type,  # type: ignore[arg-type]
        event_version=envelope.event_version,
        occurred_at=envelope.occurred_at,
        order_id=data.order_id,
        payment_attempt_id=data.payment_attempt_id,
        attempt_key=data.attempt_key,
        stripe_session_id=data.stripe_session_id,
        stripe_event_id=data.stripe_event_id,
        amount_minor=data.amount_minor,
        currency=data.currency.upper(),
        mode=data.mode,
        payload=normalized_payload,
    )
