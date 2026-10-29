from fastapi import APIRouter, HTTPException, status

from app.dependencies import DbSession
from app.modules.catalog import service
from app.modules.catalog.schemas import ProductResponse
from app.modules.catalog.service import ProductNotFound

router = APIRouter(prefix="/products", tags=["catalog"])


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
