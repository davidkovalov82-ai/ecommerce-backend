"""Главен модул за стартиране на FastAPI приложението на Cart."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import cart_router, health_router, internal_router
from app.db import db


def _sanitize_error_input(value: Any) -> Any:
    """Санира входните данни в грешките."""
    if isinstance(value, dict):
        return {
            k: _sanitize_error_input(v)
            for k, v in value.items()
            if "password" not in str(k).lower()
        }
    if isinstance(value, list):
        return [_sanitize_error_input(item) for item in value]
    return value


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Управление на жизнения цикъл на Cart приложението."""
    try:
        yield
    finally:
        db.close()


def create_app() -> FastAPI:
    """Фабрична функция за създаване и конфигуриране на Cart приложението."""
    app = FastAPI(
        title="Cart API",
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        sanitized_errors = []
        for err in exc.errors():
            err_dict = dict(err)
            if "ctx" in err_dict and isinstance(err_dict["ctx"], dict):
                err_dict["ctx"] = {
                    k: str(v) if isinstance(v, Exception) else v
                    for k, v in err_dict["ctx"].items()
                }
            if "input" in err_dict:
                err_dict["input"] = _sanitize_error_input(err_dict["input"])
            sanitized_errors.append(err_dict)
        return JSONResponse(status_code=422, content={"detail": sanitized_errors})

    app.include_router(health_router)
    app.include_router(cart_router)
    app.include_router(internal_router)
    return app


app = create_app()
