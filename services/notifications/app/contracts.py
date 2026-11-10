"""Строги договори и валидация на входящите събития от RabbitMQ."""

import datetime
import json
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

SUPPORTED_EVENT_VERSION = 1
PG_INT_MAX = 2**31 - 1
PG_BIGINT_MAX = 2**63 - 1


class EventContractError(Exception):
    """Базова грешка при невалидно или неподдържано събитие (подлежи на DLQ).

    Не съхранява сурови стойности на event_id, event_type или event_version от
    недоверени невалидни съобщения, за да не изтичат контакти или произволни низове в логовете.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidEventContract(EventContractError):
    """Съобщението не е валиден JSON или нарушава схемата и границите за БД."""


class UnsupportedEventVersion(EventContractError):
    """Непозната версия на контракта на събитието."""


class UnsupportedEventType(EventContractError):
    """Непознат тип събитие."""


def _normalize_optional_contact(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _safe_error_field(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "unknown"
    loc = errors[0].get("loc") or ()
    parts = [str(part) for part in loc if isinstance(part, (str, int))]
    return ".".join(parts)[:64] or "unknown"


class OrderPaidData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: Annotated[int, StrictInt()] = Field(gt=0, le=PG_BIGINT_MAX)
    customer_email: EmailStr | None = None
    customer_phone: StrictStr | None = None
    total_minor: Annotated[int, StrictInt()] = Field(ge=0, le=PG_BIGINT_MAX)
    currency: StrictStr = Field(min_length=3, max_length=3)
    created_at: str | None = None

    @field_validator("customer_email", mode="before")
    @classmethod
    def _empty_email_to_none(cls, v: Any) -> Any:
        return _normalize_optional_contact(v)

    @field_validator("customer_phone", mode="before")
    @classmethod
    def _empty_phone_to_none(cls, v: Any) -> Any:
        return _normalize_optional_contact(v)


class ShipmentItemData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_id: Annotated[int, StrictInt()] = Field(gt=0, le=PG_INT_MAX)
    product_title: StrictStr = Field(min_length=1, max_length=255)
    quantity: Annotated[int, StrictInt()] = Field(gt=0, le=PG_INT_MAX)


class ShipmentShippedData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: Annotated[int, StrictInt()] = Field(gt=0, le=PG_BIGINT_MAX)
    shipment_id: Annotated[int, StrictInt()] = Field(gt=0, le=PG_BIGINT_MAX)
    seller_id: Annotated[int, StrictInt()] | None = Field(
        default=None, gt=0, le=PG_INT_MAX
    )
    customer_email: EmailStr | None = None
    customer_phone: StrictStr | None = None
    items: list[ShipmentItemData] = Field(min_length=1)

    @field_validator("customer_email", mode="before")
    @classmethod
    def _empty_email_to_none(cls, v: Any) -> Any:
        return _normalize_optional_contact(v)

    @field_validator("customer_phone", mode="before")
    @classmethod
    def _empty_phone_to_none(cls, v: Any) -> Any:
        return _normalize_optional_contact(v)


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: StrictStr
    event_type: StrictStr
    event_version: Annotated[int, StrictInt()]
    occurred_at: datetime.datetime
    producer: StrictStr | None = None
    data: dict[str, Any]

    @field_validator("event_id")
    @classmethod
    def _validate_uuid(cls, v: str) -> str:
        parsed = uuid.UUID(v)
        return str(parsed)


@dataclass(frozen=True)
class ValidatedEvent:
    """Валидирано домейново събитие, готово за запис в Notifications БД."""

    event_id: str
    event_type: Literal["order.paid", "shipment.shipped"]
    event_version: int
    occurred_at: datetime.datetime
    order_id: int
    shipment_id: int | None
    payload: dict[str, Any]
    customer_email: str | None
    customer_phone: str | None


def parse_and_validate_event(raw_body: bytes) -> ValidatedEvent:
    """Разчита суровите байтове на AMQP съобщението и валидира контракта строго.

    При невалиден JSON, препълване извън БД типовете, непозната версия или тип
    хвърля EventContractError без изтичане на сурови входни стойности или контакти.
    """
    try:
        raw_obj = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidEventContract("invalid_json") from exc

    if not isinstance(raw_obj, dict):
        raise InvalidEventContract("envelope_must_be_object")

    raw_event_version = raw_obj.get("event_version")

    # Проверка на версията преди останалата валидация на payload (без логване на сурови стойности)
    if isinstance(raw_event_version, bool) or not isinstance(raw_event_version, int):
        raise InvalidEventContract("invalid_event_version_type")
    if raw_event_version != SUPPORTED_EVENT_VERSION:
        raise UnsupportedEventVersion("unsupported_event_version")

    try:
        envelope = EventEnvelope.model_validate(raw_obj)
    except ValidationError as exc:
        raise InvalidEventContract(
            f"invalid_envelope:{_safe_error_field(exc)}"
        ) from exc

    if envelope.event_type == "order.paid":
        try:
            paid_data = OrderPaidData.model_validate(envelope.data)
        except ValidationError as exc:
            raise InvalidEventContract(
                f"invalid_order_paid_payload:{_safe_error_field(exc)}"
            ) from exc

        normalized_payload = {
            "order_id": paid_data.order_id,
            "customer_email": str(paid_data.customer_email)
            if paid_data.customer_email
            else None,
            "customer_phone": paid_data.customer_phone,
            "total_minor": paid_data.total_minor,
            "currency": paid_data.currency.upper(),
            "created_at": paid_data.created_at,
        }
        return ValidatedEvent(
            event_id=envelope.event_id,
            event_type="order.paid",
            event_version=envelope.event_version,
            occurred_at=envelope.occurred_at,
            order_id=paid_data.order_id,
            shipment_id=None,
            payload=normalized_payload,
            customer_email=normalized_payload["customer_email"],
            customer_phone=paid_data.customer_phone,
        )

    if envelope.event_type == "shipment.shipped":
        try:
            shipped_data = ShipmentShippedData.model_validate(envelope.data)
        except ValidationError as exc:
            raise InvalidEventContract(
                f"invalid_shipment_shipped_payload:{_safe_error_field(exc)}"
            ) from exc

        normalized_payload = {
            "order_id": shipped_data.order_id,
            "shipment_id": shipped_data.shipment_id,
            "seller_id": shipped_data.seller_id,
            "customer_email": str(shipped_data.customer_email)
            if shipped_data.customer_email
            else None,
            "customer_phone": shipped_data.customer_phone,
            "items": [
                {
                    "product_id": it.product_id,
                    "product_title": it.product_title,
                    "quantity": it.quantity,
                }
                for it in shipped_data.items
            ],
        }
        return ValidatedEvent(
            event_id=envelope.event_id,
            event_type="shipment.shipped",
            event_version=envelope.event_version,
            occurred_at=envelope.occurred_at,
            order_id=shipped_data.order_id,
            shipment_id=shipped_data.shipment_id,
            payload=normalized_payload,
            customer_email=normalized_payload["customer_email"],
            customer_phone=shipped_data.customer_phone,
        )

    raise UnsupportedEventType("unsupported_event_type")
