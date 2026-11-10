import os
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
os.environ.setdefault(
    "JWT_SECRET", "ci-and-test-jwt-secret-key-at-least-32-bytes-long!"
)
os.environ.setdefault(
    "PAYMENTS_INTERNAL_AUTH_TOKEN", "test-internal-payments-token-32b!"
)

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.dependencies import get_db_session
from app.main import app
from app.modules.accounts.dependencies import get_users_client
from app.modules.accounts.models import User
from app.modules.accounts.users_client import RemoteUserInfo, UsersClient

settings.jwt_public_key = _test_public_pem
settings.jwt_algorithm = "RS256"
settings.jwt_issuer = "ecommerce-users"
settings.jwt_audience = "ecommerce"

TEST_DB_NAME = "commerce_test"
TEST_ROLE_NAME = "commerce_test_role"
TEST_ROLE_PASSWORD = "commerce_test_role_password"
COMMERCE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ALEMBIC_INI_PATH = str(COMMERCE_DIR / "alembic.ini")
DEFAULT_ALEMBIC_SCRIPT_LOCATION = str(COMMERCE_DIR / "migrations")
TEST_DATABASE_URL = (
    f"postgresql+psycopg://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
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


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """Фабрика за нови независими сесии към тестовата база данни."""
    return TestingSessionLocal


@contextmanager
def _temporary_migration_database(
    temp_db_name: str,
    *,
    alembic_ini_path: str = DEFAULT_ALEMBIC_INI_PATH,
    script_location: str | None = None,
) -> Iterator[tuple[Config, Engine]]:
    """Подготвя временна БД през admin ролята и връща (alembic_cfg, engine) с DB_USER."""
    admin_engine = create_engine(
        settings.admin_database_url, isolation_level="AUTOCOMMIT"
    )
    quoted_db = temp_db_name.replace('"', '""')
    quoted_user = settings.db_user.replace('"', '""')

    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{quoted_db}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{quoted_db}" OWNER "{quoted_user}"'))
        conn.execute(text(f'REVOKE ALL ON DATABASE "{quoted_db}" FROM PUBLIC'))
        conn.execute(
            text(f'GRANT CONNECT, TEMP ON DATABASE "{quoted_db}" TO "{quoted_user}"')
        )

    temp_db_url_obj = URL.create(
        drivername="postgresql+psycopg",
        username=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=temp_db_name,
    )
    temp_db_url = temp_db_url_obj.render_as_string(hide_password=False)
    temp_engine = create_engine(temp_db_url)
    try:
        alembic_cfg = Config(alembic_ini_path)
        if script_location is not None:
            alembic_cfg.set_main_option("script_location", script_location)
        alembic_cfg.set_main_option("sqlalchemy.url", temp_db_url.replace("%", "%%"))
        yield alembic_cfg, temp_engine
    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{quoted_db}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture
def temp_migration_db() -> Callable[..., Any]:
    """Фабрика за временни БД за миграционни тестове (admin DDL vs непривилегирована DB_USER)."""
    return _temporary_migration_database


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
    """Инициализира commerce_test с отделна тестова роля и реални Alembic миграции (upgrade head)."""
    main_engine = create_engine(
        settings.admin_database_url, isolation_level="AUTOCOMMIT"
    )
    escaped_pw = TEST_ROLE_PASSWORD.replace("'", "''")
    with main_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
        # Използваме отделна тестова роля без да пипаме пароли/права на работни роли
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

    # Изпълняваме реалните Alembic миграции през не-superuser тестовата роля
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(alembic_cfg, "head")

    yield

    test_engine.dispose()
    with main_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
        conn.execute(text(f"DROP ROLE IF EXISTS {TEST_ROLE_NAME}"))
    main_engine.dispose()


def _sync_users_sequence(conn: Any) -> None:
    """Синхронизира PostgreSQL sequence-а на users с текущото максимално ID."""
    conn.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('users', 'id'), "
            "coalesce(max(id), 1), max(id) IS NOT NULL) FROM users;"
        )
    )


def _sync_categories_sequence(conn: Any) -> None:
    """Синхронизира PostgreSQL sequence-а на categories с текущото максимално ID."""
    conn.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('categories', 'id'), "
            "coalesce(max(id), 1), max(id) IS NOT NULL) FROM categories;"
        )
    )


def _sync_shipments_sequence(conn: Any) -> None:
    """Синхронизира PostgreSQL sequence-а на shipments с текущото максимално ID."""
    conn.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('shipments', 'id'), "
            "coalesce(max(id), 1), max(id) IS NOT NULL) FROM shipments;"
        )
    )


def _cleanup_test_tables(session: Session) -> None:
    """Почиства всички тестови редове без да пипа началния каталог."""
    session.execute(text("DELETE FROM order_payment_discrepancies"))
    session.execute(text("DELETE FROM order_inbox_events"))
    session.execute(text("DELETE FROM order_outbox_events"))
    session.execute(text("DELETE FROM stripe_checkout_sessions"))
    session.execute(text("DELETE FROM stripe_processed_events"))
    session.execute(text("DELETE FROM payment_attempts"))
    session.execute(text("DELETE FROM cart_items"))
    session.execute(text("DELETE FROM order_items"))
    session.execute(text("DELETE FROM shipments"))
    _sync_shipments_sequence(session)
    session.execute(text("DELETE FROM orders"))
    session.execute(text("DELETE FROM products WHERE id != 1"))
    session.execute(
        text("UPDATE products SET active = true, category_id = NULL WHERE id = 1")
    )
    _sync_products_sequence(session)
    session.execute(text("DELETE FROM categories"))
    _sync_categories_sequence(session)
    session.execute(text("DELETE FROM password_reset_tokens"))
    session.execute(text("DELETE FROM account_moderation_logs"))
    session.execute(
        text("DELETE FROM users WHERE email != 'legacy-catalog@internal.ecommerce.com'")
    )
    _sync_users_sequence(session)
    session.commit()


@pytest.fixture
def db_session() -> Generator[Session]:
    """Изолирана сесия за тестове с автоматично почистване на новите записи."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        _cleanup_test_tables(session)
        session.close()


@pytest.fixture(autouse=True)
def clean_users_table() -> Generator[None]:
    """Гарантира почистване на таблиците преди и след всеки тест."""
    with TestingSessionLocal() as session:
        _cleanup_test_tables(session)
    yield
    with TestingSessionLocal() as session:
        _cleanup_test_tables(session)


class LocalTestUsersClient(UsersClient):
    """Тестов клиент, който извлича статус на потребителя от локалната тестова сесия."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        super().__init__(base_url="http://test-users", internal_token="test-token")
        self.session_factory = session_factory

    @contextmanager
    def _get_session(self) -> Iterator[Session]:
        override = app.dependency_overrides.get(get_db_session)
        if override:
            res = override()
            if hasattr(res, "__enter__"):
                with res as s:
                    yield s
            else:
                s = next(res)
                try:
                    yield s
                finally:
                    pass
        else:
            with self.session_factory() as s:
                yield s

    def get_user_status(self, user_id: int) -> RemoteUserInfo | None:
        with self._get_session() as session:
            user = session.get(User, user_id)
            if user is None:
                return None
            return RemoteUserInfo(
                id=user.id,
                email=user.email,
                role=user.role,
                status=user.status,
                token_version=user.token_version,
                display_name=user.display_name,
            )

    def forward_request(
        self,
        method: str,
        path: str,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        import json

        import httpx
        from pwdlib import PasswordHash

        from app.modules.accounts.repository import UserRepository
        from app.modules.accounts.service import (
            AccountBlocked,
            AccountClosed,
            AccountService,
            EmailAlreadyExists,
            InvalidAccountState,
            InvalidCredentials,
            InvalidCurrentPassword,
            InvalidResetToken,
            SamePasswordError,
            SelfModerationForbidden,
            TechnicalAccountProtected,
            UserNotFound,
        )
        from app.modules.accounts.tokens import create_access_token, decode_access_token

        with self._get_session() as session:
            repo = UserRepository(session)
            service = AccountService(
                repo, session, password_hasher=PasswordHash.recommended()
            )
            data = json.loads(content) if content else {}

            def _get_auth_user() -> User | None:
                auth = (headers or {}).get("authorization", "")
                if not auth.startswith("Bearer "):
                    return None
                token = auth[7:]
                try:
                    td = decode_access_token(
                        token,
                        public_key=settings.get_public_key(),
                        algorithm=settings.jwt_algorithm,
                        issuer=settings.jwt_issuer,
                        audience=settings.jwt_audience,
                    )
                    return repo.get_by_id(td.user_id)
                except Exception:
                    return None

            try:
                if path == "/users" and method == "POST":
                    user = service.register_user(
                        email=data["email"], password=data["password"]
                    )
                    return httpx.Response(
                        201,
                        json={
                            "id": user.id,
                            "email": user.email,
                            "role": user.role,
                            "display_name": user.display_name,
                        },
                        headers={"content-type": "application/json"},
                    )
                elif path == "/auth/login" and method == "POST":
                    user = service.authenticate_user(
                        email=data["email"], password=data["password"]
                    )
                    token = create_access_token(
                        user_id=user.id,
                        role=user.role,
                        email=user.email,
                        token_version=user.token_version,
                    )
                    return httpx.Response(
                        200,
                        json={"access_token": token, "token_type": "bearer"},
                        headers={"content-type": "application/json"},
                    )
                elif path == "/users/me" and method == "GET":
                    u = _get_auth_user()
                    if not u:
                        return httpx.Response(
                            401,
                            json={
                                "detail": {
                                    "code": "AUTHENTICATION_REQUIRED",
                                    "message": "Authentication required",
                                }
                            },
                        )
                    return httpx.Response(
                        200,
                        json={
                            "id": u.id,
                            "email": u.email,
                            "role": u.role,
                            "display_name": u.display_name,
                        },
                        headers={"content-type": "application/json"},
                    )
                elif path == "/users/me" and method == "PATCH":
                    u = _get_auth_user()
                    if not u:
                        return httpx.Response(
                            401,
                            json={
                                "detail": {
                                    "code": "AUTHENTICATION_REQUIRED",
                                    "message": "Authentication required",
                                }
                            },
                        )
                    updated = service.update_user_profile(
                        user_id=u.id, display_name=data.get("display_name")
                    )
                    return httpx.Response(
                        200,
                        json={
                            "id": updated.id,
                            "email": updated.email,
                            "role": updated.role,
                            "display_name": updated.display_name,
                        },
                        headers={"content-type": "application/json"},
                    )
                elif path == "/users/me/change-password" and method == "POST":
                    u = _get_auth_user()
                    if not u:
                        return httpx.Response(
                            401,
                            json={
                                "detail": {
                                    "code": "AUTHENTICATION_REQUIRED",
                                    "message": "Authentication required",
                                }
                            },
                        )
                    service.change_password(
                        user_id=u.id,
                        current_password=data["current_password"],
                        new_password=data["new_password"],
                    )
                    return httpx.Response(204)
                elif path.startswith("/users/") and path.endswith("/block"):
                    u = _get_auth_user()
                    if not u or u.role != "admin":
                        return httpx.Response(
                            403,
                            json={
                                "detail": {
                                    "code": "FORBIDDEN",
                                    "message": "Admin privileges required",
                                }
                            },
                        )
                    target_id = int(path.split("/")[2])
                    blocked = service.block_user(
                        admin_id=u.id,
                        target_user_id=target_id,
                        reason=data.get("reason"),
                    )
                    return httpx.Response(
                        200,
                        json={
                            "id": blocked.id,
                            "email": blocked.email,
                            "status": blocked.status,
                            "message": "Account has been blocked",
                        },
                        headers={"content-type": "application/json"},
                    )
                elif path.startswith("/users/") and path.endswith("/unblock"):
                    u = _get_auth_user()
                    if not u or u.role != "admin":
                        return httpx.Response(
                            403,
                            json={
                                "detail": {
                                    "code": "FORBIDDEN",
                                    "message": "Admin privileges required",
                                }
                            },
                        )
                    target_id = int(path.split("/")[2])
                    unblocked = service.unblock_user(
                        admin_id=u.id,
                        target_user_id=target_id,
                        reason=data.get("reason"),
                    )
                    return httpx.Response(
                        200,
                        json={
                            "id": unblocked.id,
                            "email": unblocked.email,
                            "status": unblocked.status,
                            "message": "Account has been unblocked",
                        },
                        headers={"content-type": "application/json"},
                    )
                elif path.startswith("/users/") and path.endswith("/close"):
                    u = _get_auth_user()
                    if not u or u.role != "admin":
                        return httpx.Response(
                            403,
                            json={
                                "detail": {
                                    "code": "FORBIDDEN",
                                    "message": "Admin privileges required",
                                }
                            },
                        )
                    target_id = int(path.split("/")[2])
                    closed = service.close_user(
                        admin_id=u.id,
                        target_user_id=target_id,
                        reason=data.get("reason"),
                    )
                    return httpx.Response(
                        200,
                        json={
                            "id": closed.id,
                            "email": closed.email,
                            "status": closed.status,
                            "message": "Account has been closed",
                        },
                        headers={"content-type": "application/json"},
                    )
                elif path == "/auth/password-reset/request" and method == "POST":
                    service.request_password_reset(email=data.get("email", ""))
                    return httpx.Response(
                        202,
                        json={
                            "message": "If the account exists and is eligible, password reset instructions have been sent."
                        },
                        headers={"content-type": "application/json"},
                    )
                elif path == "/auth/password-reset/confirm" and method == "POST":
                    service.confirm_password_reset(
                        token=data.get("token", ""),
                        new_password=data.get("new_password", ""),
                    )
                    return httpx.Response(204)
            except EmailAlreadyExists as exc:
                return httpx.Response(
                    409,
                    json={
                        "detail": {
                            "code": "EMAIL_ALREADY_EXISTS",
                            "message": str(exc),
                        }
                    },
                )
            except InvalidCredentials as exc:
                return httpx.Response(
                    401,
                    json={
                        "detail": {
                            "code": "INVALID_CREDENTIALS",
                            "message": str(exc),
                        }
                    },
                )
            except AccountBlocked as exc:
                return httpx.Response(
                    403,
                    json={
                        "detail": {"code": "ACCOUNT_BLOCKED", "message": str(exc)}
                    },
                )
            except AccountClosed as exc:
                return httpx.Response(
                    403,
                    json={
                        "detail": {"code": "ACCOUNT_CLOSED", "message": str(exc)}
                    },
                )
            except UserNotFound as exc:
                return httpx.Response(
                    404,
                    json={"detail": {"code": "USER_NOT_FOUND", "message": str(exc)}},
                )
            except InvalidCurrentPassword as exc:
                return httpx.Response(
                    400,
                    json={
                        "detail": {
                            "code": "INVALID_CURRENT_PASSWORD",
                            "message": str(exc),
                        }
                    },
                )
            except SamePasswordError as exc:
                return httpx.Response(
                    422,
                    json={"detail": {"code": "SAME_PASSWORD", "message": str(exc)}},
                )
            except SelfModerationForbidden as exc:
                return httpx.Response(
                    400,
                    json={
                        "detail": {
                            "code": "SELF_MODERATION_FORBIDDEN",
                            "message": str(exc),
                        }
                    },
                )
            except TechnicalAccountProtected as exc:
                return httpx.Response(
                    400,
                    json={
                        "detail": {
                            "code": "TECHNICAL_ACCOUNT_PROTECTED",
                            "message": str(exc),
                        }
                    },
                )
            except InvalidAccountState as exc:
                return httpx.Response(
                    400,
                    json={
                        "detail": {
                            "code": "INVALID_ACCOUNT_STATE",
                            "message": str(exc),
                        }
                    },
                )
            except InvalidResetToken as exc:
                return httpx.Response(
                    400,
                    json={
                        "detail": {
                            "code": "INVALID_RESET_TOKEN",
                            "message": str(exc),
                        }
                    },
                )

            return httpx.Response(
                404, json={"detail": {"code": "NOT_FOUND", "message": "Not found"}}
            )


@pytest.fixture(autouse=True)
def override_db_dependency() -> Generator[None]:
    """Пренасочва FastAPI зависимостта get_db_session и get_users_client към commerce_test за всички тестове."""

    def _get_test_db() -> Generator[Session]:
        with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db
    app.dependency_overrides[get_users_client] = lambda: LocalTestUsersClient(
        TestingSessionLocal
    )
    yield
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_users_client, None)
