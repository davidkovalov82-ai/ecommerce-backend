from collections.abc import Generator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.dependencies import get_db_session
from app.main import app

TEST_DB_NAME = "commerce_test"
TEST_DATABASE_URL = (
    f"postgresql+psycopg://{settings.db_user}:{settings.db_password}"
    f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
)

# Проверка за сигурност: тестовият URL задължително трябва да сочи към commerce_test
assert TEST_DB_NAME in TEST_DATABASE_URL
assert settings.db_name != TEST_DB_NAME, (
    "Основната база не бива да се казва commerce_test"
)

test_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(
    bind=test_engine, autoflush=False, expire_on_commit=False
)


def _sync_products_sequence(conn: Any) -> None:
    """Синхронизира PostgreSQL sequence-а на products с текущото максимално ID."""
    conn.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('products', 'id'), "
            "coalesce(max(id), 1), max(id) IS NOT NULL) FROM products;"
        )
    )


@pytest.fixture(scope="session", autouse=True)
def setup_test_database() -> Generator[None]:
    """Инициализира commerce_test чрез реални Alembic миграции (upgrade head)."""
    # 1. Пълно пресъздаване само на отделната commerce_test база
    main_engine = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    with main_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {TEST_DB_NAME}"))

    # 2. Изпълняваме реалните Alembic миграции до последната (head)
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(alembic_cfg, "head")

    # 3. Синхронизираме sequence след първата миграция, която вече вмъква id=1
    with test_engine.begin() as conn:
        _sync_products_sequence(conn)

    yield

    # 4. Почистване след тестовата сесия
    with main_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))


def _sync_users_sequence(conn: Any) -> None:
    """Синхронизира PostgreSQL sequence-а на users с текущото максимално ID."""
    conn.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('users', 'id'), "
            "coalesce(max(id), 1), max(id) IS NOT NULL) FROM users;"
        )
    )


@pytest.fixture
def db_session() -> Generator[Session]:
    """Изолирана сесия за тестове с автоматично почистване на новите записи."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        # Премахваме всички добавени от тестовете редове
        session.execute(text("DELETE FROM users"))
        _sync_users_sequence(session)
        session.execute(text("DELETE FROM products WHERE id != 1"))
        _sync_products_sequence(session)
        session.commit()
        session.close()


@pytest.fixture(autouse=True)
def clean_users_table() -> Generator[None]:
    """Гарантира почистване на таблицата users преди и след всеки тест."""
    with TestingSessionLocal() as session:
        session.execute(text("DELETE FROM users"))
        _sync_users_sequence(session)
        session.commit()
    yield
    with TestingSessionLocal() as session:
        session.execute(text("DELETE FROM users"))
        _sync_users_sequence(session)
        session.commit()


@pytest.fixture(autouse=True)
def override_db_dependency() -> Generator[None]:
    """Пренасочва FastAPI зависимостта get_db_session към commerce_test за всички тестове."""

    def _get_test_db() -> Generator[Session]:
        with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db
    yield
    app.dependency_overrides.pop(get_db_session, None)
