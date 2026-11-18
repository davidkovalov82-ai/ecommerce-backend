"""HTTP обработчици за Catalog (публични и вътрешни)."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.dependencies import (
    AdminUserDep,
    CatalogServiceDep,
    CurrentUserDep,
    verify_internal_auth,
)
from app.schemas import (
    CategoryCreateRequest,
    CategoryResponse,
    CategoryUpdateRequest,
    ProductBatchRequest,
    ProductCreateRequest,
    ProductInternalDetails,
    ProductListResponse,
    ProductResponse,
    ProductUpdateRequest,
    StockReleaseRequest,
    StockReleaseResponse,
    StockReserveRequest,
    StockReserveResponse,
)
from app.service import (
    CategoryInUse,
    CategoryNameAlreadyExists,
    CategoryNotFound,
    InsufficientStock,
    InvalidCategoryId,
    InvalidReservationItems,
    ProductNotFound,
    ProductUnavailable,
    ReservationConflict,
)

categories_router = APIRouter(prefix="/categories", tags=["categories"])
products_router = APIRouter(prefix="/products", tags=["products"])
seller_router = APIRouter(prefix="/users/me/products", tags=["seller-products"])
internal_router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(verify_internal_auth)],
)
health_router = APIRouter(tags=["health"])


@health_router.get("/health")
def health() -> dict[str, str]:
    """Проверка за работоспособност на Catalog сервиса."""
    return {"status": "ok"}


# --- Публични категории ---


@categories_router.get("", response_model=list[CategoryResponse])
def list_categories(
    catalog_service: CatalogServiceDep,
) -> list[CategoryResponse]:
    """Публичен списък с категории."""
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
    """Създаване на нова категория (само администратори)."""
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
    """Преименуване на категория (само администратори)."""
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
    """Изтриване на празна категория (само администратори)."""
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


# --- Публични продукти ---


@products_router.post(
    "", response_model=ProductResponse, status_code=status.HTTP_201_CREATED
)
def create_product(
    data: ProductCreateRequest,
    catalog_service: CatalogServiceDep,
    current_user: CurrentUserDep,
) -> ProductResponse:
    """Създаване на продукт от автентикиран потребител."""
    try:
        product = catalog_service.create_product(
            seller_id=current_user.user_id,
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


@products_router.patch("/{product_id}", response_model=ProductResponse)
def update_product(
    product_id: int,
    data: ProductUpdateRequest,
    catalog_service: CatalogServiceDep,
    current_user: CurrentUserDep,
) -> ProductResponse:
    """Частично обновяване на собствен продукт."""
    update_data = data.model_dump(exclude_unset=True)
    try:
        product = catalog_service.update_product(
            product_id=product_id,
            seller_id=current_user.user_id,
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


@products_router.get("", response_model=ProductListResponse)
def list_products(
    catalog_service: CatalogServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    category_id: int | None = Query(default=None),
) -> ProductListResponse:
    """Публичен списък с активни продукти."""
    products = catalog_service.list_active_products(
        limit=limit, offset=offset, category_id=category_id
    )
    return ProductListResponse(
        items=[ProductResponse.model_validate(p) for p in products],
        limit=limit,
        offset=offset,
    )


@products_router.get("/{product_id}", response_model=ProductResponse)
def get_product(
    product_id: int,
    catalog_service: CatalogServiceDep,
) -> ProductResponse:
    """Извличане на активен продукт по ID."""
    try:
        product = catalog_service.get_product_by_id(product_id)
        return ProductResponse.model_validate(product)
    except ProductNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )


# --- Продукти на текущия продавач ---


@seller_router.get("", response_model=ProductListResponse)
def get_my_products(
    current_user: CurrentUserDep,
    catalog_service: CatalogServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProductListResponse:
    """Извличане на продукти на текущия продавач."""
    products = catalog_service.list_products_by_seller(
        seller_id=current_user.user_id,
        limit=limit,
        offset=offset,
    )
    return ProductListResponse(
        items=[ProductResponse.model_validate(p) for p in products],
        limit=limit,
        offset=offset,
    )


# --- Вътрешен API договор за Cart и Orders ---


@internal_router.get(
    "/products/{product_id}", response_model=ProductInternalDetails | None
)
def get_internal_product(
    product_id: int,
    catalog_service: CatalogServiceDep,
) -> ProductInternalDetails:
    """Вътрешно извличане на продукт за Cart/Orders."""
    product = catalog_service.get_product_internal(product_id)
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )
    return product


@internal_router.post("/products/batch", response_model=list[ProductInternalDetails])
def get_internal_products_batch(
    req: ProductBatchRequest,
    catalog_service: CatalogServiceDep,
) -> list[ProductInternalDetails]:
    """Пакетно извличане на детайли за продукти."""
    return catalog_service.get_products_internal_batch(req.product_ids)


@internal_router.post(
    "/stock/reserve",
    response_model=StockReserveResponse,
    status_code=status.HTTP_200_OK,
)
def reserve_stock(
    req: StockReserveRequest,
    catalog_service: CatalogServiceDep,
) -> StockReserveResponse:
    """Атомарно и идемпотентно резервиране на наличност от Orders."""
    try:
        catalog_service.reserve_stock(order_id=req.order_id, items=req.items)
        return StockReserveResponse(status="reserved", order_id=req.order_id)
    except InsufficientStock as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "INSUFFICIENT_STOCK",
                "product_id": exc.product_id,
                "message": f"Insufficient stock for product {exc.product_id}",
            },
        )
    except ProductUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PRODUCT_UNAVAILABLE",
                "product_id": exc.product_id,
                "message": f"Product {exc.product_id} is unavailable or seller is inactive",
            },
        )
    except ReservationConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RESERVATION_CONFLICT",
                "message": str(exc),
            },
        )
    except InvalidReservationItems as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_RESERVATION_ITEMS",
                "message": str(exc),
            },
        )


@internal_router.post(
    "/stock/release",
    response_model=StockReleaseResponse,
    status_code=status.HTTP_200_OK,
)
def release_stock(
    req: StockReleaseRequest,
    catalog_service: CatalogServiceDep,
) -> StockReleaseResponse:
    """Идемпотентно освобождаване на наличност от Orders."""
    catalog_service.release_stock(order_id=req.order_id)
    return StockReleaseResponse(status="released", order_id=req.order_id)
