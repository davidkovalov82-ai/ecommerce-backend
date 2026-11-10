"""Общи pytest фикстури за изолираната тестова база users_test."""

import os
from collections.abc import Generator
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Генерираме изолирана RSA 2048 тестова двойка ключове за тестовата среда
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

os.environ["JWT_PRIVATE_KEY"] = _test_private_pem
os.environ["JWT_PUBLIC_KEY"] = _test_public_pem
os.environ["JWT_ALGORITHM"] = "RS256"
os.environ["JWT_ISSUER"] = "ecommerce-users"
os.environ["JWT_AUDIENCE"] = "ecommerce"

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.dependencies import get_db_session
from app.main import app
from app.models import User

# Задаваме ключовете директно в инстанцията на settings за текущия pytest процес
settings.jwt_private_key = _test_private_pem
settings.jwt_public_key = _test_public_pem

TEST_DB_NAME = "users_test"
TEST_ROLE_NAME = "users_test_role"
TEST_ROLE_PASSWORD = "users_test_role_password"
TEST_DATABASE_URL = (
    f"postgresql+psycopg://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
    f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
)

assert settings.db_name != TEST_DB_NAME, "Основната БД не бива да е users_test"

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
    """Инициализира users_test с отделна не-superuser роля и Alembic upgrade head."""
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

    users_dir = Path(__file__).resolve().parent.parent
    alembic_ini_path = str(users_dir / "alembic.ini")
    alembic_cfg = Config(alembic_ini_path)
    alembic_cfg.set_main_option("script_location", str(users_dir / "migrations"))
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
    """Връща независима SQLAlchemy сесия към тестовата база."""
    with TestingSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def clean_users_table() -> Generator[None]:
    """Изчиства таблицата users преди и след всеки тест."""

    def _clean() -> None:
        with TestingSessionLocal() as session:
            session.execute(text("DELETE FROM account_moderation_logs"))
            session.execute(text("DELETE FROM password_reset_tokens"))
            session.execute(text("DELETE FROM users"))
            session.commit()

    _clean()
    yield
    _clean()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient]:
    """FastAPI TestClient с пренасочена сесия към тестовата база данни."""

    def override_get_db_session() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def test_rsa_keys() -> tuple[str, str]:
    """Връща двойката (private_pem, public_pem) за проверка на токени в тестовете."""
    return _test_private_pem, _test_public_pem


def create_user_and_token(
    db_session: Session,
    email: str = "user@example.com",
    password: str = "DefaultPassword12345!",
    role: str = "customer",
    status: str = "active",
    display_name: str | None = None,
) -> tuple[User, str]:
    """Създава потребител в БД и издава валиден RS256 JWT токен за тестове."""
    from pwdlib import PasswordHash

    from app.tokens import create_access_token

    hasher = PasswordHash.recommended()
    user = User(
        email=email,
        password_hash=hasher.hash(password),
        role=role,
        status=status,
        display_name=display_name,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    token = create_access_token(
        user_id=user.id,
        role=user.role,
        email=user.email,
        token_version=user.token_version,
        private_key=_test_private_pem,
        algorithm="RS256",
        issuer="ecommerce-users",
        audience="ecommerce",
    )
    return user, token


def create_admin(
    db_session: Session,
    email: str = "admin@example.com",
    password: str = "AdminPassword12345!",
) -> tuple[User, str]:
    """Създава потребител с роля admin и валиден RS256 токен."""
    return create_user_and_token(
        db_session=db_session,
        email=email,
        password=password,
        role="admin",
    )
