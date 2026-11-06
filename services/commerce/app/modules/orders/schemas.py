import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class CreateOrderRequest(BaseModel):
    """Схема за тялото на заявка за оформяне на поръчка.

    Изисква празен JSON обект {} и забранява допълнителни полета.
    """

    model_config = ConfigDict(extra="forbid")


class OrderItemResponse(BaseModel):
    """Схема за позиция от поръчка със запазените исторически стойности."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    product_id: int
    product_title: str
    quantity: int
    unit_price_minor: int
    subtotal_minor: int


class OrderSummaryResponse(BaseModel):
    """Обобщена схема за поръчка в списъчен изглед (без вложени позиции)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    currency: str
    total_minor: int
    created_at: datetime.datetime


class OrderDetailResponse(BaseModel):
    """Детайлна схема за поръчка с включени нейни позиции."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    currency: str
    total_minor: int
    created_at: datetime.datetime
    items: list[OrderItemResponse]


class OrderListResponse(BaseModel):
    """Пагиниран отговор със списък от обобщени поръчки на потребителя."""

    items: list[OrderSummaryResponse]
    total: int
    limit: int
    offset: int


class CreatePaymentRequest(BaseModel):
    """Схема за заявка за симулирано плащане на поръчка."""

    model_config = ConfigDict(extra="forbid")

    payment_method: Literal["test_success", "test_declined"]


class PaymentAttemptResponse(BaseModel):
    """Схема за отговор от опит за плащане на поръчка."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    status: str
    amount_minor: int
    currency: str


class StripeCheckoutSessionResponse(BaseModel):
    """Схема за отговор при създаване на Stripe Checkout сесия."""

    model_config = ConfigDict(from_attributes=True)

    checkout_url: str
    session_id: str
