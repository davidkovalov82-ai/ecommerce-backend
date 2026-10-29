from fastapi import APIRouter, HTTPException, Query, status

from app.dependencies import DbSession
from app.modules.catalog import service
from app.modules.catalog.schemas import ProductListResponse, ProductResponse
from app.modules.catalog.service import ProductNotFound

router = APIRouter(prefix="/products", tags=["catalog"])


@router.get("", response_model=ProductListResponse)
def list_products(
    session: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProductListResponse:
    """HTTP обработчик за получаване на публичен списък с активни продукти."""
    products = service.list_active_products(session, limit=limit, offset=offset)
    return ProductListResponse(
        items=[ProductResponse.model_validate(p) for p in products],
        limit=limit,
        offset=offset,
    )


@router.get("/{product_id}", response_model=ProductResponse)
def get_product(product_id: int, session: DbSession) -> ProductResponse:
    """HTTP обработчик за получаване на продукт по ID от базата данни."""
    try:
        product = service.get_product_by_id(session, product_id)
        return ProductResponse.model_validate(product)
    except ProductNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )
