from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import Database, db, engine
from app.dependencies import get_db_session


def test_settings_load() -> None:
    """Проверка, че настройките за БД се зареждат правилно."""
    assert settings.db_host is not None
    assert settings.db_port == 5432
    assert "postgresql+psycopg://" in settings.database_url


def test_database_connection_select_one() -> None:
    """Проверка, че връзката към реалната PostgreSQL база успешно изпълнява SELECT 1."""
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar()
        assert result == 1


def test_database_class_lifecycle() -> None:
    """Проверка на методите на новия Database клас (engine, session, close)."""
    custom_db = Database(settings.database_url, pool_pre_ping=True)
    assert custom_db.engine is not None

    with custom_db.session() as s:
        result = s.execute(text("SELECT 1")).scalar()
        assert result == 1

    with patch.object(custom_db._engine, "dispose") as mock_dispose:
        custom_db.close()
        assert mock_dispose.call_count == 1


def test_db_session_lifecycle_closes_on_success() -> None:
    """Проверка, че сесията се създава и затваря надеждно при успешен сценарий."""
    mock_session = MagicMock()

    with patch.object(db, "_sessionmaker", return_value=mock_session):
        gen = get_db_session()
        session = next(gen)

        assert session is mock_session
        assert mock_session.close.call_count == 0

        # Симулираме завършване на HTTP заявката
        try:
            next(gen)
        except StopIteration:
            pass

        assert mock_session.close.call_count == 1


def test_db_session_lifecycle_closes_on_exception() -> None:
    """Проверка, че сесията се затваря надеждно дори ако възникне грешка по време на заявката."""
    mock_session = MagicMock()

    with patch.object(db, "_sessionmaker", return_value=mock_session):
        gen = get_db_session()
        _ = next(gen)

        assert mock_session.close.call_count == 0

        # Симулираме изключение по време на обработка на заявка
        try:
            gen.throw(RuntimeError("Simulated error in handler"))
        except RuntimeError:
            pass

        assert mock_session.close.call_count == 1


@pytest.mark.anyio
async def test_fastapi_lifespan_closes_db() -> None:
    """Проверка, че FastAPI lifespan извиква db.close() при спиране на сървъра."""
    from app.main import app, lifespan

    with patch("app.main.db.close") as mock_close:
        async with lifespan(app):
            pass
        assert mock_close.call_count == 1
