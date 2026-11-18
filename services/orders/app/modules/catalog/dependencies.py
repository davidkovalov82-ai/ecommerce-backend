from typing import Annotated

from fastapi import Depends

from app.dependencies import DbSession
from app.modules.catalog.client import CatalogClient
from app.modules.catalog.repository import (
    CategoryRepository,
    ProductRepository,
    SellerStatusRepository,
)
from app.modules.catalog.service import CatalogService


def get_catalog_client() -> CatalogClient:
    """Фабрика за CatalogClient."""
    return CatalogClient()


def resolve_catalog_client(explicit: CatalogClient | None = None) -> CatalogClient:
    """Връща подадения клиент, текущия override от app.dependency_overrides или нов CatalogClient."""
    if explicit is not None:
        return explicit
    from app.main import app

    override = app.dependency_overrides.get(get_catalog_client)
    if override is not None:
        return override()
    return CatalogClient()


CatalogClientDep = Annotated[CatalogClient, Depends(get_catalog_client)]


def get_product_repository(session: DbSession) -> ProductRepository:
    """Фабрика за ProductRepository с текущата сесия на заявката."""
    return ProductRepository(session)


def get_category_repository(session: DbSession) -> CategoryRepository:
    """Фабрика за CategoryRepository с текущата сесия на заявката."""
    return CategoryRepository(session)


def get_seller_status_repository(session: DbSession) -> SellerStatusRepository:
    """Фабрика за SellerStatusRepository с текущата сесия на заявката."""
    return SellerStatusRepository(session)


def get_catalog_service(
    session: DbSession,
    product_repository: Annotated[ProductRepository, Depends(get_product_repository)],
    category_repository: Annotated[
        CategoryRepository, Depends(get_category_repository)
    ],
) -> CatalogService:
    """Фабрика за CatalogService с инжектирани хранилища и обща сесия за заявката."""
    return CatalogService(
        product_repository=product_repository,
        category_repository=category_repository,
        session=session,
    )


CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]
