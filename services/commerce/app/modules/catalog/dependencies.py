from typing import Annotated

from fastapi import Depends

from app.dependencies import DbSession
from app.modules.catalog.repository import ProductRepository
from app.modules.catalog.service import CatalogService


def get_product_repository(session: DbSession) -> ProductRepository:
    """Фабрика за ProductRepository с текущата сесия на заявката."""
    return ProductRepository(session)


def get_catalog_service(
    product_repository: Annotated[ProductRepository, Depends(get_product_repository)],
) -> CatalogService:
    """Фабрика за CatalogService с инжектирано хранилище."""
    return CatalogService(product_repository)


CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]
