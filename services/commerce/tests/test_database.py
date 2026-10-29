from unittest.mock import MagicMock, patch

from sqlalchemy import text

from app.config import settings
from app.db import engine
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


def test_db_session_lifecycle_closes_on_success() -> None:
    """Проверка, че сесията се създава и затваря надеждно при успешен сценарий."""
    mock_session = MagicMock()

    with patch("app.dependencies.SessionLocal", return_value=mock_session):
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

    with patch("app.dependencies.SessionLocal", return_value=mock_session):
        gen = get_db_session()
        _ = next(gen)

        assert mock_session.close.call_count == 0

        # Симулираме изключение по време на обработка на заявка
        try:
            gen.throw(RuntimeError("Simulated error in handler"))
        except RuntimeError:
            pass

        assert mock_session.close.call_count == 1
