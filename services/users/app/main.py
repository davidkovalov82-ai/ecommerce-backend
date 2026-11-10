"""Главно FastAPI приложение за микросервиса Users."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import auth_router, internal_router, router
from app.db import db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Управление на ресурсите при стартиране и спиране на Users."""
    yield
    db.close()


app = FastAPI(
    title="Users Service",
    description="Микросервис за регистрация, автентикация и управление на потребители.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(auth_router)
app.include_router(internal_router)
