from fastapi import APIRouter, HTTPException, Query, status

from app.modules.accounts.dependencies import AdminUserDep, CurrentUserDep
from app.modules.catalog.dependencies import CatalogServiceDep
from app.modules.catalog.schemas import (
    CategoryCreateRequest,
    CategoryResponse,
    CategoryUpdateRequest,
    ProductCreateRequest,
    ProductListResponse,
    ProductResponse,
    ProductUpdateRequest,
)
from app.modules.catalog.service import (
    CategoryInUse,
    CategoryNameAlreadyExists,
    CategoryNotFound,
    InvalidCategoryId,
    ProductNotFound,
)

categories_router = APIRouter(prefix="/categories", tags=["categories"])
router = APIRouter(prefix="/products", tags=["catalog"])


@categories_router.get("", response_model=list[CategoryResponse])
def list_categories(
    catalog_service: CatalogServiceDep,
) -> list[CategoryResponse]:
    """HTTP обработчик за получаване на публичен списък с категории със стабилна сортировка."""
    categories = catalog_service.list_categories()
    return [CategoryResponse.model_validate(c) for c in categories]


@categories_router.post(
    "",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_category(
    data: CategoryCreateRequest,
    catalog_service: CatalogServiceDep,
    admin_user: AdminUserDep,
) -> CategoryResponse:
    """HTTP обработчик за създаване на нова категория (само за администратори)."""
    try:
        category = catalog_service.create_category(name=data.name)
        return CategoryResponse.model_validate(category)
    except CategoryNameAlreadyExists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CATEGORY_NAME_EXISTS",
                "message": "Category with this name already exists",
            },
        )


@categories_router.patch("/{category_id}", response_model=CategoryResponse)
def update_category(
    category_id: int,
    data: CategoryUpdateRequest,
    catalog_service: CatalogServiceDep,
    admin_user: AdminUserDep,
) -> CategoryResponse:
    """HTTP обработчик за преименуване на категория (само за администратори)."""
    try:
        category = catalog_service.update_category(
            category_id=category_id, name=data.name
        )
        return CategoryResponse.model_validate(category)
    except CategoryNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CATEGORY_NOT_FOUND", "message": "Category not found"},
        )
    except CategoryNameAlreadyExists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CATEGORY_NAME_EXISTS",
                "message": "Category with this name already exists",
            },
        )


@categories_router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: int,
    catalog_service: CatalogServiceDep,
    admin_user: AdminUserDep,
) -> None:
    """HTTP обработчик за изтриване на категория без продукти (само за администратори)."""
    try:
        catalog_service.delete_category(category_id=category_id)
    except CategoryNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CATEGORY_NOT_FOUND", "message": "Category not found"},
        )
    except CategoryInUse:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CATEGORY_IN_USE",
                "message": "Category cannot be deleted because it is assigned to products",
            },
        )


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def create_product(
    data: ProductCreateRequest,
    catalog_service: CatalogServiceDep,
    current_user: CurrentUserDep,
) -> ProductResponse:
    """HTTP обработчик за създаване на нов продукт от автентикиран потребител."""
    try:
        product = catalog_service.create_product(
            seller_id=current_user.id,
            title=data.title,
            price_minor=data.price_minor,
            currency=data.currency,
            stock=data.stock,
            active=data.active,
            category_id=data.category_id,
        )
        return ProductResponse.model_validate(product)
    except InvalidCategoryId:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_CATEGORY", "message": "Category not found"},
        )


@router.patch("/{product_id}", response_model=ProductResponse)
def update_product(
    product_id: int,
    data: ProductUpdateRequest,
    catalog_service: CatalogServiceDep,
    current_user: CurrentUserDep,
) -> ProductResponse:
    """HTTP обработчик за частично обновяване на собствен продукт (само за собственика)."""
    update_data = data.model_dump(exclude_unset=True)
    try:
        product = catalog_service.update_product(
            product_id=product_id,
            seller_id=current_user.id,
            update_data=update_data,
        )
        return ProductResponse.model_validate(product)
    except ProductNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )
    except InvalidCategoryId:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_CATEGORY", "message": "Category not found"},
        )


@router.get("", response_model=ProductListResponse)
def list_products(
    catalog_service: CatalogServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    category_id: int | None = Query(default=None),
) -> ProductListResponse:
    """HTTP обработчик за получаване на публичен списък с активни продукти."""
    products = catalog_service.list_active_products(
        limit=limit, offset=offset, category_id=category_id
    )
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
    """HTTP обработчик за получаване на продукт по ID от базата данни (само активни)."""
    try:
        product = catalog_service.get_product_by_id(product_id)
        return ProductResponse.model_validate(product)
    except ProductNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )
