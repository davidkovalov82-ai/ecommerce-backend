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


class CategoryCreateRequest(BaseModel):
    """Схема за входните данни при създаване на категория от администратор."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)

    @field_validator("name", mode="before")
    @classmethod
    def strip_and_validate_name(cls, v: object) -> object:
        """Премахва празни интервали и проверява за непразно име."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                raise ValueError("Category name cannot be empty")
            return v
        return v


class CategoryUpdateRequest(BaseModel):
    """Схема за входните данни при обновяване (преименуване) на категория от администратор."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)

    @field_validator("name", mode="before")
    @classmethod
    def strip_and_validate_name(cls, v: object) -> object:
        """Премахва празни интервали и проверява за непразно име."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                raise ValueError("Category name cannot be empty")
            return v
        return v


class CategoryResponse(BaseModel):
    """Схема за публичен отговор с данни за категория."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class ProductCreateRequest(BaseModel):
    """Схема за входните данни при създаване на продукт от автентикиран потребител."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    price_minor: StrictInt = Field(ge=0, le=2_147_483_647)
    currency: Literal["EUR"]
    stock: StrictInt = Field(ge=0, le=2_147_483_647)
    active: bool = True
    category_id: StrictInt | None = Field(default=None, ge=1)

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, v: object) -> object:
        """Премахва външните празни интервали от заглавието на продукта."""
        if isinstance(v, str):
            return v.strip()
        return v


class ProductUpdateRequest(BaseModel):
    """Схема за частично обновяване на продукт от собственика му (PATCH)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    price_minor: StrictInt | None = Field(default=None, ge=0, le=2_147_483_647)
    currency: Literal["EUR"] | None = None
    stock: StrictInt | None = Field(default=None, ge=0, le=2_147_483_647)
    active: StrictBool | None = None
    category_id: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def validate_payload(cls, data: Any) -> Any:
        """Отхвърля празен JSON обект и изрично подадени null стойности за задължителните полета."""
        if not isinstance(data, dict):
            return data
        if not data:
            raise ValueError("Payload must contain at least one field to update")
        for key, value in data.items():
            if key != "category_id" and value is None:
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
    seller_id: int
    category_id: int | None = None
    title: str
    price_minor: int
    currency: str
    stock: int
    active: bool


class ProductListResponse(BaseModel):
    items: list[ProductResponse]
    limit: int
    offset: int
