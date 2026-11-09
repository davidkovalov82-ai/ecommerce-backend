import datetime
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ShippingAddress(BaseModel):
    """Схема за входните данни на адреса за доставка при оформяне на поръчка."""

    model_config = ConfigDict(extra="forbid")

    recipient_name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=1, max_length=50)
    country_code: str = Field(min_length=2, max_length=2)
    city: str = Field(min_length=1, max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    address_line1: str = Field(min_length=1, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)

    @field_validator(
        "recipient_name",
        "phone",
        "country_code",
        "city",
        "postal_code",
        "address_line1",
        mode="before",
    )
    @classmethod
    def validate_non_empty_and_no_null_bytes(cls, v: object) -> object:
        """Проверява за липса на нулеви байтове и непразни стойности след изчистване."""
        if isinstance(v, str):
            if "\x00" in v:
                raise ValueError("Null bytes are not allowed")
            trimmed = v.strip()
            if not trimmed:
                raise ValueError("Field cannot be empty or whitespace-only")
            return trimmed
        return v

    @field_validator("address_line2", mode="before")
    @classmethod
    def validate_address_line2(cls, v: object) -> object:
        """Валидира незадължителния втори ред на адреса."""
        if v is None:
            return None
        if isinstance(v, str):
            if "\x00" in v:
                raise ValueError("Null bytes are not allowed")
            trimmed = v.strip()
            if not trimmed:
                return None
            return trimmed
        return v

    @field_validator("country_code")
    @classmethod
    def validate_country_code(cls, v: str) -> str:
        """Проверява дали кодът на държавата се състои от точно 2 ASCII букви и нормализира към главни букви."""
        if not re.fullmatch(r"[A-Za-z]{2}", v):
            raise ValueError("country_code must be exactly 2 ASCII letters (e.g. 'BG')")
        return v.upper()


class ShippingAddressResponse(BaseModel):
    """Схема за адреса за доставка в отговорите на поръчки и пратки."""

    model_config = ConfigDict(from_attributes=True)

    recipient_name: str
    phone: str
    country_code: str
    city: str
    postal_code: str
    address_line1: str
    address_line2: str | None = None


class CreateOrderRequest(BaseModel):
    """Схема за тялото на заявка за оформяне на поръчка (POST /orders)."""

    model_config = ConfigDict(extra="forbid")

    shipping_address: ShippingAddress


class OrderItemResponse(BaseModel):
    """Схема за позиция от поръчка със запазените исторически стойности."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    product_id: int
    seller_id: int | None = None
    product_title: str
    quantity: int
    unit_price_minor: int
    subtotal_minor: int


class OrderShipmentResponse(BaseModel):
    """Схема за пратка в детайлния изглед на поръчката за купувача."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    seller_id: int
    status: str
    created_at: datetime.datetime
    shipped_at: datetime.datetime | None = None
    items: list[OrderItemResponse] = []


class OrderSummaryResponse(BaseModel):
    """Обобщена схема за поръчка в списъчен изглед (без вложени позиции)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    fulfillment_status: str = "unshipped"
    currency: str
    total_minor: int
    created_at: datetime.datetime
    expires_at: datetime.datetime | None = None


class OrderDetailResponse(BaseModel):
    """Детайлна схема за поръчка с включени нейни позиции, адрес и пратки."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    fulfillment_status: str = "unshipped"
    currency: str
    total_minor: int
    created_at: datetime.datetime
    expires_at: datetime.datetime | None = None
    shipping_address: ShippingAddressResponse | None = None
    items: list[OrderItemResponse]
    shipments: list[OrderShipmentResponse] = []


class OrderListResponse(BaseModel):
    """Пагиниран отговор със списък от обобщени поръчки на потребителя."""

    items: list[OrderSummaryResponse]
    total: int
    limit: int
    offset: int


class SellerShipmentItemResponse(BaseModel):
    """Схема за позиция от пратка за конкретния продавач."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    product_title: str
    quantity: int
    unit_price_minor: int
    subtotal_minor: int


class SellerShipmentSummaryResponse(BaseModel):
    """Обобщена схема за пратка в списъчния изглед на продавача."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    seller_id: int
    status: str
    created_at: datetime.datetime
    shipped_at: datetime.datetime | None = None
    items_count: int
    items: list[SellerShipmentItemResponse]


class SellerShipmentDetailResponse(BaseModel):
    """Детайлна схема за пратка в изгледа на продавача (позиции + адрес)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    seller_id: int
    status: str
    created_at: datetime.datetime
    shipped_at: datetime.datetime | None = None
    shipping_address: ShippingAddressResponse | None = None
    items: list[SellerShipmentItemResponse]


class SellerShipmentListResponse(BaseModel):
    """Пагиниран списък от пратки на продавача."""

    items: list[SellerShipmentSummaryResponse]
    total: int
    limit: int
    offset: int


class StripeCheckoutSessionResponse(BaseModel):
    """Схема за отговор при създаване на Stripe Checkout сесия."""

    model_config = ConfigDict(from_attributes=True)

    checkout_url: str
    session_id: str
