"""Входна точка на FastAPI приложението за независимия Payments сервис."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health_router, internal_router, webhook_router
from app.db import db


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    try:
        yield
    finally:
        db.close()


def create_app() -> FastAPI:
    """Създава и конфигурира Payments FastAPI приложението."""
    application = FastAPI(
        title="Payments Service API",
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,
    )
    application.include_router(health_router)
    application.include_router(internal_router)
    application.include_router(webhook_router)
    return application


app = create_app()
