"""Pytest фикстури за изолираната тестова база и тестова роля на Notifications."""

import os
from collections.abc import Generator

os.environ.setdefault("DB_USER", "notifications")
os.environ.setdefault("DB_PASSWORD", "notifications")
os.environ.setdefault("DB_NAME", "notifications")
os.environ.setdefault("DB_ADMIN_USER", "commerce")
os.environ.setdefault("DB_ADMIN_PASSWORD", "commerce")
os.environ.setdefault("DB_ADMIN_NAME", "commerce")

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

TEST_DB_NAME = "notifications_test"
TEST_ROLE_NAME = "notifications_test_role"
TEST_ROLE_PASSWORD = "notifications_test_role_password"
TEST_DATABASE_URL = (
    f"postgresql+psycopg://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
    f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
)

assert settings.db_name != TEST_DB_NAME, (
    "Основната база не бива да се казва notifications_test"
)

test_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autoflush=False,
    expire_on_commit=False,
)


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """Фабрика за нови независими сесии към тестовата БД на Notifications."""
    return TestingSessionLocal


@pytest.fixture(scope="session", autouse=True)
def setup_test_database() -> Generator[None]:
    """Инициализира notifications_test с отделна тестова роля (без да пипа работните роли)."""
    admin_engine = create_engine(
        settings.admin_database_url,
        isolation_level="AUTOCOMMIT",
    )
    escaped_pw = TEST_ROLE_PASSWORD.replace("'", "''")
    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
        conn.execute(text(f"DROP ROLE IF EXISTS {TEST_ROLE_NAME}"))
        conn.execute(
            text(
                f"CREATE ROLE {TEST_ROLE_NAME} "
                f"WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{escaped_pw}'"
            )
        )
        conn.execute(text(f"CREATE DATABASE {TEST_DB_NAME} OWNER {TEST_ROLE_NAME}"))
        conn.execute(text(f"REVOKE ALL ON DATABASE {TEST_DB_NAME} FROM PUBLIC"))
        conn.execute(
            text(f"GRANT CONNECT, TEMP ON DATABASE {TEST_DB_NAME} TO {TEST_ROLE_NAME}")
        )

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(alembic_cfg, "head")

    yield

    test_engine.dispose()
    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
        conn.execute(text(f"DROP ROLE IF EXISTS {TEST_ROLE_NAME}"))
    admin_engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables() -> Generator[None]:
    """Почиства таблиците преди и след всеки тест."""
    with TestingSessionLocal() as session:
        session.execute(text("DELETE FROM notification_deliveries"))
        session.execute(text("DELETE FROM inbox_events"))
        session.commit()
    yield
    with TestingSessionLocal() as session:
        session.execute(text("DELETE FROM notification_deliveries"))
        session.execute(text("DELETE FROM inbox_events"))
        session.commit()


@pytest.fixture
def db_session() -> Generator[Session]:
    """Отворена сесия към тестовата БД на Notifications."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
