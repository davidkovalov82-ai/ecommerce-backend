from fastapi import APIRouter, HTTPException, Query, status

from app.modules.accounts.dependencies import AdminUserDep
from app.modules.catalog.dependencies import CatalogServiceDep
from app.modules.catalog.schemas import (
    ProductCreateRequest,
    ProductListResponse,
    ProductResponse,
)
from app.modules.catalog.service import ProductNotFound

router = APIRouter(prefix="/products", tags=["catalog"])


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def create_product(
    data: ProductCreateRequest,
    catalog_service: CatalogServiceDep,
    _: AdminUserDep,
) -> ProductResponse:
    """HTTP обработчик за създаване на нов продукт (само за администратори)."""
    product = catalog_service.create_product(
        title=data.title,
        price_minor=data.price_minor,
        currency=data.currency,
        stock=data.stock,
        active=data.active,
    )
    return ProductResponse.model_validate(product)


@router.get("", response_model=ProductListResponse)
def list_products(
    catalog_service: CatalogServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProductListResponse:
    """HTTP обработчик за получаване на публичен списък с активни продукти."""
    products = catalog_service.list_active_products(limit=limit, offset=offset)
    return ProductListResponse(
        items=[ProductResponse.model_validate(p) for p in products],
        limit=limit,
        offset=offset,
    )


@router.get("/{product_id}", response_model=ProductResponse)
def get_product(
    product_id: int,
    catalog_service: CatalogServiceDep,
) -> ProductResponse:
    """HTTP обработчик за получаване на продукт по ID от базата данни."""
    try:
        product = catalog_service.get_product_by_id(product_id)
        return ProductResponse.model_validate(product)
    except ProductNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )
