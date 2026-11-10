from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db import db
from app.modules.accounts.api import auth_router
from app.modules.accounts.api import router as accounts_router
from app.modules.cart.api import router as cart_router
from app.modules.catalog.api import categories_router
from app.modules.catalog.api import router as catalog_router
from app.modules.orders.api import router as orders_router
from app.modules.orders.api import seller_shipments_router


def _sanitize_error_input(value: Any, is_sensitive_path: bool) -> Any:
    """Рекурсивно премахва пароли и чувствителни данни от input полето на грешката."""
    if isinstance(value, dict):
        return {
            k: _sanitize_error_input(v, is_sensitive_path)
            for k, v in value.items()
            if "password" not in str(k).lower()
        }
    if isinstance(value, list):
        if is_sensitive_path and all(
            not isinstance(item, (dict, list)) for item in value
        ):
            return []
        return [_sanitize_error_input(item, is_sensitive_path) for item in value]
    if is_sensitive_path and isinstance(value, (str, bytes, int, float, bool)):
        return None
    return value


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Управление на жизнения цикъл на приложението."""
    try:
        yield
    finally:
        # Грациозно освобождаване на връзките при спиране на сървъра
        db.close()


def create_app() -> FastAPI:
    """Фабрична функция за създаване и конфигуриране на приложението."""
    app = FastAPI(
        title="Commerce API",
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Предпазва от изтичане на пароли в тялото на грешките 422."""
        path = request.url.path.lower()
        is_sensitive_path = any(
            segment in path for segment in ("user", "auth", "login", "password")
        )

        sanitized_errors = []
        for err in exc.errors():
            err_dict = dict(err)
            if "ctx" in err_dict and isinstance(err_dict["ctx"], dict):
                err_dict["ctx"] = {
                    k: str(v) if isinstance(v, Exception) else v
                    for k, v in err_dict["ctx"].items()
                }
            loc = err_dict.get("loc", ())

            # 1. Ако локацията на грешката съдържа "password", премахваме input изцяло
            if any("password" in str(part).lower() for part in loc):
                err_dict.pop("input", None)
            elif "input" in err_dict:
                # 2. При грешна структура на тялото (напр. подаден стринг или списък вместо JSON обект)
                if loc in (("body",), ["body"]) and not isinstance(
                    err_dict["input"], dict
                ):
                    err_dict.pop("input", None)
                else:
                    # 3. Рекурсивно саниране на речници и списъци в input
                    sanitized_val = _sanitize_error_input(
                        err_dict["input"], is_sensitive_path
                    )
                    if sanitized_val is None:
                        err_dict.pop("input", None)
                    else:
                        err_dict["input"] = sanitized_val

            sanitized_errors.append(err_dict)
        return JSONResponse(status_code=422, content={"detail": sanitized_errors})

    app.include_router(categories_router)
    app.include_router(catalog_router)
    app.include_router(accounts_router)
    app.include_router(auth_router)
    app.include_router(cart_router)
    app.include_router(orders_router)
    app.include_router(seller_shipments_router)
    return app


app = create_app()
