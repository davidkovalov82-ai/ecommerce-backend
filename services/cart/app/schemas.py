"""Pydantic схеми за заявки и отговори в услугата Cart."""

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class CartItemPayload(BaseModel):
    """Схема за заявка за задаване на количество за продукт в количката."""

    model_config = ConfigDict(extra="forbid")

    quantity: StrictInt = Field(
        ...,
        ge=1,
        le=99,
        description="Количество от дадения продукт в количката (цяло число между 1 и 99).",
    )


# Алиас за съвместимост с предишни имена
CartItemUpdateRequest = CartItemPayload


class CartItemResponse(BaseModel):
    """Схема за позиция в количката с обогатени данни от Catalog и версия на записа."""

    model_config = ConfigDict(from_attributes=True)

    product_id: int
    title: str
    quantity: int
    unit_price_minor: int
    subtotal_minor: int
    available: bool
    version: int = 1


class CartResponse(BaseModel):
    """Схема за цялостното състояние на потребителската количка."""

    model_config = ConfigDict(from_attributes=True)

    items: list[CartItemResponse]
    total_minor: int
    currency: str = "EUR"


class ClearPurchasedItem(BaseModel):
    """Позиция за изчистване с очаквана версия от снимката на поръчката."""

    model_config = ConfigDict(extra="forbid")

    product_id: int
    version: int = Field(gt=0)


class ClearPurchasedRequest(BaseModel):
    """Заявка от Orders за изчистване на успешно закупени позиции от количката."""

    model_config = ConfigDict(extra="forbid")

    order_id: int = Field(gt=0)
    items: list[ClearPurchasedItem]


class ClearPurchasedResponse(BaseModel):
    """Отговор при успешно изчистване на закупени позиции."""

    status: str = "cleared"
    user_id: int
    cleared_product_ids: list[int]
    order_id: int


class ProductInternalDetails(BaseModel):
    """Модел за данни на продукт, получен през вътрешния API на Catalog."""

    id: int
    seller_id: int
    category_id: int | None = None
    title: str
    price_minor: int
    currency: str
    stock: int
    active: bool
    seller_active: bool = True
