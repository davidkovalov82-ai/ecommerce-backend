from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


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
