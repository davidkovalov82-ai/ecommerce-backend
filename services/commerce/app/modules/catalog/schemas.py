from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)


class ProductCreateRequest(BaseModel):
    """Схема за входните данни при създаване на продукт от администратор."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    price_minor: StrictInt = Field(ge=0, le=2_147_483_647)
    currency: Literal["EUR"]
    stock: StrictInt = Field(ge=0, le=2_147_483_647)
    active: bool = True

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, v: object) -> object:
        """Премахва външните празни интервали от заглавието на продукта."""
        if isinstance(v, str):
            return v.strip()
        return v


class ProductUpdateRequest(BaseModel):
    """Схема за частично обновяване на продукт от администратор (PATCH)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    price_minor: StrictInt | None = Field(default=None, ge=0, le=2_147_483_647)
    currency: Literal["EUR"] | None = None
    stock: StrictInt | None = Field(default=None, ge=0, le=2_147_483_647)
    active: StrictBool | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_payload(cls, data: Any) -> Any:
        """Отхвърля празен JSON обект и изрично подадени null стойности."""
        if not isinstance(data, dict):
            return data
        if not data:
            raise ValueError("Payload must contain at least one field to update")
        for key, value in data.items():
            if value is None:
                raise ValueError(f"Field '{key}' cannot be null")
        return data

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, v: object) -> object:
        """Премахва външните празни интервали от заглавието на продукта."""
        if isinstance(v, str):
            return v.strip()
        return v


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    price_minor: int
    currency: str
    stock: int
    active: bool


class ProductListResponse(BaseModel):
    items: list[ProductResponse]
    limit: int
    offset: int
