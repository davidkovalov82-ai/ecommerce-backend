"""HTTP обработчици за публични маршрути на Catalog в Commerce.

След K6.2 микросервисът Catalog е единствен собственик на стоки, категории,
цените, остатъците и видимостта. Commerce запазва публичните си URL адреси
и проксира заявките към Catalog.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.modules.catalog.client import (
    CatalogClient,
    CatalogServiceUnavailableError,
)
from app.modules.catalog.dependencies import get_catalog_client
from app.modules.catalog.schemas import (
    CategoryResponse,
    ProductListResponse,
    ProductResponse,
)

categories_router = APIRouter(prefix="/categories", tags=["categories"])
router = APIRouter(prefix="/products", tags=["catalog"])


async def _forward(
    request: Request,
    path: str,
    method: str,
    catalog_client: CatalogClient,
    body: bytes | None = None,
) -> Response:
    """Пренасочва публична заявка към самостоятелния Catalog микросервис."""
    headers = dict(request.headers)
    client_ip = request.client.host if request.client else None
    if client_ip:
        headers["x-forwarded-for"] = client_ip

    try:
        resp = catalog_client.forward_request(
            method=method,
            path=path,
            content=body,
            headers=headers,
            params=dict(request.query_params),
        )
    except CatalogServiceUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "SERVICE_UNAVAILABLE",
                "message": "Catalog service is unavailable",
            },
        )

    # Премахваме content-length за избягване на конфликти при стрийминг
    resp_headers = dict(resp.headers)
    resp_headers.pop("content-length", None)
    resp_headers.pop("transfer-encoding", None)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=resp.headers.get("content-type"),
    )


# --- Категории ---


@categories_router.get("", response_model=list[CategoryResponse])
async def list_categories(
    request: Request,
    catalog_client: Annotated[CatalogClient, Depends(get_catalog_client)],
) -> Response:
    return await _forward(request, "/categories", "GET", catalog_client)


@categories_router.post(
    "",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_category(
    request: Request,
    catalog_client: Annotated[CatalogClient, Depends(get_catalog_client)],
) -> Response:
    body = await request.body()
    return await _forward(request, "/categories", "POST", catalog_client, body=body)


@categories_router.patch("/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: int,
    request: Request,
    catalog_client: Annotated[CatalogClient, Depends(get_catalog_client)],
) -> Response:
    body = await request.body()
    return await _forward(
        request, f"/categories/{category_id}", "PATCH", catalog_client, body=body
    )


@categories_router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: int,
    request: Request,
    catalog_client: Annotated[CatalogClient, Depends(get_catalog_client)],
) -> Response:
    return await _forward(
        request, f"/categories/{category_id}", "DELETE", catalog_client
    )


# --- Продукти ---


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    request: Request,
    catalog_client: Annotated[CatalogClient, Depends(get_catalog_client)],
) -> Response:
    body = await request.body()
    return await _forward(request, "/products", "POST", catalog_client, body=body)


@router.patch("/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: int,
    request: Request,
    catalog_client: Annotated[CatalogClient, Depends(get_catalog_client)],
) -> Response:
    body = await request.body()
    return await _forward(
        request, f"/products/{product_id}", "PATCH", catalog_client, body=body
    )


@router.get("", response_model=ProductListResponse)
async def list_products(
    request: Request,
    catalog_client: Annotated[CatalogClient, Depends(get_catalog_client)],
) -> Response:
    return await _forward(request, "/products", "GET", catalog_client)


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: int,
    request: Request,
    catalog_client: Annotated[CatalogClient, Depends(get_catalog_client)],
) -> Response:
    return await _forward(request, f"/products/{product_id}", "GET", catalog_client)
