"""Общи pytest фикстури за изолираната тестова база payments_test."""

import os
from collections.abc import Generator

os.environ.setdefault(
    "PAYMENTS_INTERNAL_AUTH_TOKEN", "test-internal-payments-token-32b!"
)

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.main import app

TEST_DB_NAME = "payments_test"
TEST_ROLE_NAME = "payments_test_role"
TEST_ROLE_PASSWORD = "payments_test_role_password"
TEST_DATABASE_URL = (
    f"postgresql+psycopg://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
    f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
)

assert settings.db_name != TEST_DB_NAME, "Основната БД не бива да е payments_test"

test_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autoflush=False,
    expire_on_commit=False,
)


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    return TestingSessionLocal


@pytest.fixture(scope="session", autouse=True)
def setup_test_database() -> Generator[None]:
    """Инициализира payments_test с отделна не-superuser роля и Alembic upgrade head."""
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


@pytest.fixture
def db_session() -> Generator[Session]:
    with TestingSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def clean_payments_tables() -> Generator[None]:
    def _clean() -> None:
        with TestingSessionLocal() as session:
            session.execute(text("DELETE FROM payment_outbox_events"))
            session.execute(text("DELETE FROM stripe_processed_events"))
            session.execute(text("DELETE FROM stripe_checkout_sessions"))
            session.execute(text("DELETE FROM payment_attempts"))
            session.commit()

    _clean()
    yield
    _clean()


@pytest.fixture(autouse=True)
def override_db_dependency() -> Generator[None]:
    from app.dependencies import get_db_session

    def _get_test_db() -> Generator[Session]:
        with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db
    yield
    app.dependency_overrides.pop(get_db_session, None)
