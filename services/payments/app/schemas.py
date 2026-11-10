"""Pydantic схеми за вътрешното HTTP API на Payments."""

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class CheckoutLineItemPayload(BaseModel):
    """Позиция за Stripe Checkout, изчислена от Commerce от запазената поръчка."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    unit_amount: StrictInt = Field(ge=0)
    quantity: StrictInt = Field(ge=1)
    currency: Literal["EUR", "eur"] = "eur"


class InternalCheckoutSessionRequest(BaseModel):
    """Заявка от Commerce към Payments за създаване/извличане на Checkout Session."""

    model_config = ConfigDict(extra="forbid")

    order_id: StrictInt = Field(gt=0)
    attempt_key: str = Field(min_length=1, max_length=100)
    amount_minor: StrictInt = Field(ge=0)
    currency: Literal["EUR"]
    line_items: list[CheckoutLineItemPayload] = Field(min_length=1)
    order_expires_at: datetime.datetime | None = None


class InternalCheckoutSessionResponse(BaseModel):
    """Отговор към Commerce с актуалната Stripe Checkout сесия за текущата попытка."""

    payment_attempt_id: int
    session_id: str
    checkout_url: str
    status: str


class InternalCancelPaymentRequest(BaseModel):
    """Заявка от Commerce за проверка/прекратяване на активна сесия при отказ или изтичане."""

    model_config = ConfigDict(extra="forbid")

    reason: Literal["cancel", "reserve_expired"] = "cancel"
    order_expires_at: datetime.datetime | None = None


class InternalCancelPaymentResponse(BaseModel):
    """Резултат от проверката и прекратяването на сесия в Stripe."""

    status: Literal["can_cancel", "already_expired"]
    action: str
