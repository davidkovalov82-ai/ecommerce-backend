from pydantic import BaseModel, ConfigDict, Field, StrictInt


class CartItemUpdateRequest(BaseModel):
    """Схема за заявка за обновяване или добавяне на позиция в количката."""

    model_config = ConfigDict(extra="forbid")

    quantity: StrictInt = Field(
        ...,
        ge=1,
        le=99,
        description="Количество от дадения продукт в количката (цяло число между 1 и 99).",
    )


class CartItemResponse(BaseModel):
    """Схема за единична позиция в потребителската количка."""

    model_config = ConfigDict(from_attributes=True)

    product_id: int
    title: str
    quantity: int
    unit_price_minor: int
    subtotal_minor: int
    available: bool


class CartResponse(BaseModel):
    """Схема за пълен отговор за състоянието на потребителската количка."""

    model_config = ConfigDict(from_attributes=True)

    items: list[CartItemResponse]
    total_minor: int
    currency: str = "EUR"

