from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.modules.accounts.api import router as accounts_router
from app.modules.catalog.api import router as catalog_router


def create_app() -> FastAPI:
    """Фабрична функция за създаване и конфигуриране на приложението."""
    app = FastAPI(
        title="Commerce API",
        version="0.1.0",
        docs_url="/docs",
    )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Предпазва от изтичане на пароли в тялото на грешките 422."""
        sanitized_errors = []
        for err in exc.errors():
            err_dict = dict(err)
            if "password" in err_dict.get("loc", ()):
                err_dict.pop("input", None)
            sanitized_errors.append(err_dict)
        return JSONResponse(status_code=422, content={"detail": sanitized_errors})

    app.include_router(catalog_router)
    app.include_router(accounts_router)
    return app


app = create_app()
