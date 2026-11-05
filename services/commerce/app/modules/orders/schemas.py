import datetime

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

