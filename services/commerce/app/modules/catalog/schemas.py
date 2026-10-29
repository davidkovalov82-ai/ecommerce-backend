from pydantic import BaseModel, ConfigDict


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
