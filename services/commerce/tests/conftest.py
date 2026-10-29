from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import Base
from app.dependencies import get_db_session
from app.main import app
from app.modules.catalog.models import Product

TEST_DATABASE_URL = (
    f"postgresql+psycopg://{settings.db_user}:{settings.db_password}"
    f"@{settings.db_host}:{settings.db_port}/commerce_test"
)

test_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
def setup_test_database() -> Generator[None]:
    """Инициализира отделната тестова база commerce_test за цялата тестова сесия."""
    main_engine = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    with main_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname='commerce_test'")
        ).scalar()
        if not exists:
            conn.execute(text("CREATE DATABASE commerce_test"))

    # Пресъздаваме чиста схема в тестовата база
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    # Подготвяме продукта за API тестовете
    with TestingSessionLocal() as session:
        session.add(
            Product(
                id=1,
                title="Notebook",
                price_minor=1299,
                currency="EUR",
                stock=10,
                active=True,
            )
        )
        session.commit()

    yield

    # Почистване след тестовата сесия
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session() -> Generator[Session]:
    """Изолирана сесия за тестове с гарантиран rollback накрая."""
    with TestingSessionLocal() as session:
        yield session
        session.rollback()


@pytest.fixture(autouse=True)
def override_db_dependency() -> Generator[None]:
    """Гарантира, че API клиентът в тестовете използва отделната тестова база."""

    def _get_test_db() -> Generator[Session]:
        with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db
    yield
    app.dependency_overrides.pop(get_db_session, None)
