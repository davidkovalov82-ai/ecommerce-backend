from typing import Annotated

from fastapi import Depends

from app.modules.catalog.client import CatalogClient


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
