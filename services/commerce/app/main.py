from fastapi import FastAPI

from app.modules.catalog.api import router as catalog_router


def create_app() -> FastAPI:
    """Фабрична функция за създаване и конфигуриране на приложението."""
    app = FastAPI(
        title="Commerce API",
        version="0.1.0",
        docs_url="/docs",
    )
    app.include_router(catalog_router)
    return app


app = create_app()
