"""Общи pytest фикстури за изолираната тестова база catalog_test."""

import os
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# Генерираме изолирана RSA 2048 тестова двойка ключове
_test_private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
)
_test_private_pem = _test_private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")
_test_public_pem = (
    _test_private_key.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode("utf-8")
)

os.environ["JWT_PUBLIC_KEY"] = _test_public_pem
os.environ["JWT_ALGORITHM"] = "RS256"
os.environ["JWT_ISSUER"] = "ecommerce-users"
os.environ["JWT_AUDIENCE"] = "ecommerce"

from app.config import settings
from app.dependencies import get_db_session, get_users_client
from app.main import app
from app.users_client import RemoteUserInfo, UsersServiceUnavailableError

settings.jwt_public_key = _test_public_pem


class _InternalHeaders(dict[str, str]):
    def __getitem__(self, key: str) -> str:
        if key == "X-Internal-Token":
            return settings.internal_auth_token
        return super().__getitem__(key)

    def get(self, key: str, default: object = None) -> object:
        if key == "X-Internal-Token":
            return settings.internal_auth_token
        return super().get(key, default)

    def copy(self) -> dict[str, str]:
        return {"X-Internal-Token": settings.internal_auth_token}


INTERNAL_HEADERS = _InternalHeaders({"X-Internal-Token": settings.internal_auth_token})

TEST_DB_NAME = "catalog_test"
TEST_ROLE_NAME = "catalog_test_role"
TEST_ROLE_PASSWORD = "catalog_test_role_password"
TEST_DATABASE_URL = (
    f"postgresql+psycopg://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
    f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
)

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
    """Инициализира catalog_test с отделна роля и Alembic upgrade head."""
    admin_engine = create_engine(
        settings.admin_database_url,
        isolation_level="AUTOCOMMIT",
    )
    escaped_pw = TEST_ROLE_PASSWORD.replace("'", "''")
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
        conn.execute(text(f'DROP ROLE IF EXISTS "{TEST_ROLE_NAME}"'))
        conn.execute(
            text(
                f'CREATE ROLE "{TEST_ROLE_NAME}" '
                f"WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{escaped_pw}'"
            )
        )
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}" OWNER "{TEST_ROLE_NAME}"'))
        conn.execute(text(f'REVOKE ALL ON DATABASE "{TEST_DB_NAME}" FROM PUBLIC'))
        conn.execute(
            text(
                f'GRANT CONNECT, TEMP ON DATABASE "{TEST_DB_NAME}" TO "{TEST_ROLE_NAME}"'
            )
        )

    catalog_dir = Path(__file__).resolve().parent.parent
    alembic_ini_path = str(catalog_dir / "alembic.ini")
    alembic_cfg = Config(alembic_ini_path)
    alembic_cfg.set_main_option("script_location", str(catalog_dir / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(alembic_cfg, "head")

    yield

    test_engine.dispose()
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
        conn.execute(text(f'DROP ROLE IF EXISTS "{TEST_ROLE_NAME}"'))
    admin_engine.dispose()


@pytest.fixture
def db_session() -> Generator[Session]:
    """Връща сесия към тестовата база."""
    with TestingSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def clean_tables() -> Generator[None]:
    """Изчиства таблиците в Catalog преди и след всеки тест."""

    def _clean() -> None:
        with TestingSessionLocal() as session:
            session.execute(text("DELETE FROM stock_reservations"))
            session.execute(text("DELETE FROM seller_inbox_events"))
            session.execute(text("DELETE FROM seller_statuses"))
            session.execute(text("DELETE FROM products"))
            session.execute(text("DELETE FROM categories"))
            session.commit()

    _clean()
    yield
    _clean()


class MockUsersClient:
    """Mock за клиент към Users в тестовете."""

    def __init__(self) -> None:
        self.unavailable = False
        self.users: dict[int, RemoteUserInfo] = {}

    def set_user(
        self,
        user_id: int,
        role: str = "customer",
        status: str = "active",
        token_version: int = 0,
        email: str | None = None,
    ) -> None:
        self.users[user_id] = RemoteUserInfo(
            id=user_id,
            email=email or f"user{user_id}@example.com",
            role=role,
            status=status,
            token_version=token_version,
        )

    def get_user_status(self, user_id: int) -> RemoteUserInfo | None:
        if self.unavailable:
            raise UsersServiceUnavailableError("Users mock is set to unavailable")
        if user_id in self.users:
            return self.users[user_id]
        role = "admin" if user_id == 1 else "customer"
        return RemoteUserInfo(
            id=user_id,
            email=f"user{user_id}@example.com",
            role=role,
            status="active",
            token_version=0,
        )


@pytest.fixture
def mock_users_client() -> Generator[MockUsersClient]:
    """Предоставя мок на UsersClient за TestClient."""
    mock = MockUsersClient()
    app.dependency_overrides[get_users_client] = lambda: mock
    yield mock
    app.dependency_overrides.pop(get_users_client, None)


@pytest.fixture
def client(
    db_session: Session, mock_users_client: MockUsersClient
) -> Generator[TestClient]:
    """FastAPI TestClient с пренасочена сесия и mock Users клиент."""

    def override_get_db_session() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def make_auth_headers(
    user_id: int,
    role: str = "customer",
    email: str = "user@example.com",
    token_version: int = 0,
) -> dict[str, str]:
    """Генерира валидна заглавка Authorization с RS256 токен."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "email": email,
        "token_version": token_version,
        "iss": "ecommerce-users",
        "aud": "ecommerce",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    token = jwt.encode(payload, _test_private_pem, algorithm="RS256")
    return {"Authorization": f"Bearer {token}"}
