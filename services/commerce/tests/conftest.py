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
from app.modules.catalog.client import (
    CatalogClient,
    CatalogInsufficientStockError,
    CatalogProductUnavailableError,
    RemoteProductInfo,
)
from app.modules.catalog.dependencies import get_catalog_client
from app.modules.catalog.models import Product, SellerStatus

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
    session.execute(text("DELETE FROM seller_inbox_events"))
    session.execute(text("DELETE FROM seller_statuses"))
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

        from app.modules.accounts.dependencies import _password_hasher
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
            RateLimitExceeded,
            SamePasswordError,
            SelfModerationForbidden,
            TechnicalAccountProtected,
            UserNotFound,
        )
        from app.modules.accounts.tokens import (
            InvalidToken,
            create_access_token,
            decode_access_token,
        )

        with self._get_session() as session:
            repo = UserRepository(session)
            service = AccountService(repo, session, password_hasher=_password_hasher)
            data = json.loads(content) if content else {}

            def _get_auth_user() -> tuple[int, Any, dict[str, str]]:
                auth = (headers or {}).get("authorization", "")
                if not auth.startswith("Bearer "):
                    return (
                        401,
                        {
                            "detail": {
                                "code": "AUTHENTICATION_REQUIRED",
                                "message": "Authentication required",
                            }
                        },
                        {"WWW-Authenticate": "Bearer"},
                    )
                token = auth[7:].strip()
                try:
                    td = decode_access_token(
                        token,
                        public_key=settings.get_public_key(),
                        algorithm=settings.jwt_algorithm,
                        issuer=settings.jwt_issuer,
                        audience=settings.jwt_audience,
                    )
                except InvalidToken:
                    return (
                        401,
                        {
                            "detail": {
                                "code": "INVALID_TOKEN",
                                "message": "Invalid or expired token",
                            }
                        },
                        {"WWW-Authenticate": "Bearer"},
                    )
                u = repo.get_by_id(td.user_id)
                if not u:
                    return (
                        401,
                        {
                            "detail": {
                                "code": "USER_NOT_FOUND",
                                "message": "User not found",
                            }
                        },
                        {"WWW-Authenticate": "Bearer"},
                    )
                if u.token_version != td.token_version:
                    return (
                        401,
                        {
                            "detail": {
                                "code": "INVALID_TOKEN",
                                "message": "Token has been revoked",
                            }
                        },
                        {"WWW-Authenticate": "Bearer"},
                    )
                if u.status == "blocked":
                    return (
                        403,
                        {
                            "detail": {
                                "code": "ACCOUNT_BLOCKED",
                                "message": "Account is blocked",
                            }
                        },
                        {},
                    )
                if u.status == "closed":
                    return (
                        403,
                        {
                            "detail": {
                                "code": "ACCOUNT_CLOSED",
                                "message": "Account is closed",
                            }
                        },
                        {},
                    )
                if u.status != "active":
                    return (
                        403,
                        {
                            "detail": {
                                "code": "ACCOUNT_INACTIVE",
                                "message": "Account is inactive",
                            }
                        },
                        {},
                    )
                return 200, u, {}

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
                    status_c, u_or_err, r_headers = _get_auth_user()
                    if status_c != 200:
                        return httpx.Response(
                            status_c, json=u_or_err, headers=r_headers
                        )
                    return httpx.Response(
                        200,
                        json={
                            "id": u_or_err.id,
                            "email": u_or_err.email,
                            "role": u_or_err.role,
                            "display_name": u_or_err.display_name,
                        },
                        headers={"content-type": "application/json"},
                    )
                elif path == "/users/me" and method == "PATCH":
                    status_c, u_or_err, r_headers = _get_auth_user()
                    if status_c != 200:
                        return httpx.Response(
                            status_c, json=u_or_err, headers=r_headers
                        )
                    updated = service.update_user_profile(
                        user_id=u_or_err.id, display_name=data.get("display_name")
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
                    status_c, u_or_err, r_headers = _get_auth_user()
                    if status_c != 200:
                        return httpx.Response(
                            status_c, json=u_or_err, headers=r_headers
                        )
                    service.change_password(
                        user_id=u_or_err.id,
                        current_password=data["current_password"],
                        new_password=data["new_password"],
                    )
                    return httpx.Response(204)
                elif path.startswith("/users/") and path.endswith("/block"):
                    status_c, u_or_err, r_headers = _get_auth_user()
                    if status_c != 200:
                        return httpx.Response(
                            status_c, json=u_or_err, headers=r_headers
                        )
                    if u_or_err.role != "admin":
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
                        admin_id=u_or_err.id,
                        target_user_id=target_id,
                        reason=data.get("reason"),
                    )
                    from app.modules.catalog.repository import SellerStatusRepository

                    SellerStatusRepository(session).upsert_status(target_id, "blocked")
                    session.commit()
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
                    status_c, u_or_err, r_headers = _get_auth_user()
                    if status_c != 200:
                        return httpx.Response(
                            status_c, json=u_or_err, headers=r_headers
                        )
                    if u_or_err.role != "admin":
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
                        admin_id=u_or_err.id,
                        target_user_id=target_id,
                        reason=data.get("reason"),
                    )
                    from app.modules.catalog.repository import SellerStatusRepository

                    SellerStatusRepository(session).upsert_status(target_id, "active")
                    session.commit()
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
                    status_c, u_or_err, r_headers = _get_auth_user()
                    if status_c != 200:
                        return httpx.Response(
                            status_c, json=u_or_err, headers=r_headers
                        )
                    if u_or_err.role != "admin":
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
                        admin_id=u_or_err.id,
                        target_user_id=target_id,
                        reason=data.get("reason"),
                    )
                    from app.modules.catalog.repository import SellerStatusRepository

                    SellerStatusRepository(session).upsert_status(target_id, "closed")
                    session.commit()
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
            except EmailAlreadyExists:
                return httpx.Response(
                    409,
                    json={
                        "detail": {
                            "code": "EMAIL_ALREADY_EXISTS",
                            "message": "User with this email already exists",
                        }
                    },
                )
            except InvalidCredentials:
                return httpx.Response(
                    401,
                    json={
                        "detail": {
                            "code": "INVALID_CREDENTIALS",
                            "message": "Invalid email or password",
                        }
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )
            except AccountBlocked as exc:
                return httpx.Response(
                    403,
                    json={"detail": {"code": "ACCOUNT_BLOCKED", "message": str(exc)}},
                )
            except AccountClosed as exc:
                return httpx.Response(
                    403,
                    json={"detail": {"code": "ACCOUNT_CLOSED", "message": str(exc)}},
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
            except RateLimitExceeded as exc:
                return httpx.Response(
                    429,
                    json={
                        "detail": {
                            "code": "RATE_LIMIT_EXCEEDED",
                            "message": str(exc),
                        }
                    },
                )

            return httpx.Response(
                404, json={"detail": {"code": "NOT_FOUND", "message": "Not found"}}
            )


class LocalTestCatalogClient(CatalogClient):
    """Тестов клиент за Catalog, изпълняващ операциите локално през тестовата сесия."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        super().__init__(base_url="http://test-catalog", internal_token="test-token")
        self.session_factory = session_factory
        self._reservations: dict[int, list[dict[str, Any]]] = {}
        self._released_orders: set[int] = set()

    @contextmanager
    def _get_session(self) -> Iterator[Session]:
        with self.session_factory() as s:
            yield s

    def get_product(self, product_id: int) -> RemoteProductInfo | None:
        with self._get_session() as session:
            p = session.get(Product, product_id)
            if p is None:
                return None
            seller_status = session.get(SellerStatus, p.seller_id)
            is_active = (seller_status.status == "active") if seller_status else True
            return RemoteProductInfo(
                id=p.id,
                seller_id=p.seller_id,
                category_id=p.category_id,
                title=p.title,
                price_minor=p.price_minor,
                currency=p.currency,
                stock=p.stock,
                active=p.active,
                seller_active=is_active,
            )

    def get_products_batch(self, product_ids: list[int]) -> list[RemoteProductInfo]:
        if not product_ids:
            return []
        with self._get_session() as session:
            from sqlalchemy import select

            products = session.scalars(
                select(Product).where(Product.id.in_(product_ids))
            ).all()
            seller_ids = [p.seller_id for p in products]
            statuses = {
                s.seller_id: s.status
                for s in session.scalars(
                    select(SellerStatus).where(SellerStatus.seller_id.in_(seller_ids))
                ).all()
            }
            return [
                RemoteProductInfo(
                    id=p.id,
                    seller_id=p.seller_id,
                    category_id=p.category_id,
                    title=p.title,
                    price_minor=p.price_minor,
                    currency=p.currency,
                    stock=p.stock,
                    active=p.active,
                    seller_active=(statuses.get(p.seller_id, "active") == "active"),
                )
                for p in products
            ]

    def reserve_stock(self, order_id: int, items: list[dict[str, int]]) -> None:
        with self._get_session() as session:
            if order_id in self._reservations:
                return

            sorted_ids = sorted({item["product_id"] for item in items})
            from sqlalchemy import select

            products = session.scalars(
                select(Product)
                .where(Product.id.in_(sorted_ids))
                .order_by(Product.id.asc())
                .with_for_update()
            ).all()
            products_by_id = {p.id: p for p in products}

            for item in items:
                p = products_by_id.get(item["product_id"])
                if p is None or not p.active:
                    raise CatalogProductUnavailableError(item["product_id"])
                if p.stock < item["quantity"]:
                    raise CatalogInsufficientStockError(item["product_id"])

            for item in items:
                p = products_by_id[item["product_id"]]
                p.stock -= item["quantity"]

            self._reservations[order_id] = [dict(item) for item in items]
            session.commit()

    def release_stock(self, order_id: int) -> None:
        if order_id in self._released_orders:
            return
        self._released_orders.add(order_id)
        reserved_items = self._reservations.pop(order_id, None)
        with self._get_session() as session:
            if reserved_items is not None:
                for item in reserved_items:
                    p = session.get(Product, item["product_id"])
                    if p is not None:
                        p.stock += item["quantity"]
            else:
                from app.modules.orders.models import OrderItem

                items = (
                    session.query(OrderItem)
                    .filter(OrderItem.order_id == order_id)
                    .all()
                )
                for item in items:
                    p = session.get(Product, item.product_id)
                    if p is not None:
                        p.stock += item.quantity
            session.commit()

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
        from pydantic import ValidationError

        from app.modules.accounts.tokens import (
            InvalidToken,
            TokenData,
            decode_access_token,
        )
        from app.modules.catalog.repository import (
            CategoryRepository,
            ProductRepository,
        )
        from app.modules.catalog.schemas import (
            CategoryCreateRequest,
            CategoryUpdateRequest,
            ProductCreateRequest,
            ProductUpdateRequest,
        )
        from app.modules.catalog.service import (
            CatalogService,
            CategoryInUse,
            CategoryNameAlreadyExists,
            CategoryNotFound,
            InvalidCategoryId,
            ProductNotFound,
        )

        with self._get_session() as session:
            cat_repo = CategoryRepository(session)
            prod_repo = ProductRepository(session)
            service = CatalogService(prod_repo, session, cat_repo)

            def _get_auth_user() -> tuple[int, Any, dict[str, str]]:
                auth = (headers or {}).get("authorization", "")
                if not auth.startswith("Bearer "):
                    return (
                        401,
                        {
                            "detail": {
                                "code": "AUTHENTICATION_REQUIRED",
                                "message": "Authentication required",
                            }
                        },
                        {"WWW-Authenticate": "Bearer"},
                    )
                token = auth[7:].strip()
                try:
                    td = decode_access_token(
                        token,
                        public_key=settings.get_public_key(),
                        algorithm=settings.jwt_algorithm,
                        issuer=settings.jwt_issuer,
                        audience=settings.jwt_audience,
                    )
                except InvalidToken:
                    return (
                        401,
                        {
                            "detail": {
                                "code": "INVALID_TOKEN",
                                "message": "Invalid token",
                            }
                        },
                        {"WWW-Authenticate": "Bearer"},
                    )
                from app.modules.accounts.dependencies import get_users_client

                users_client_factory = app.dependency_overrides.get(get_users_client)
                users_client = users_client_factory() if users_client_factory else None
                if users_client is not None and hasattr(
                    users_client, "get_user_status"
                ):
                    from app.modules.accounts.users_client import (
                        UsersServiceUnavailableError,
                    )

                    try:
                        u_status = users_client.get_user_status(td.user_id)
                        if u_status is None:
                            return (
                                401,
                                {
                                    "detail": {
                                        "code": "USER_NOT_FOUND",
                                        "message": "User not found",
                                    }
                                },
                                {"WWW-Authenticate": "Bearer"},
                            )
                        if u_status.role != td.role:
                            td = TokenData(
                                user_id=td.user_id,
                                role=u_status.role,
                                email=td.email,
                                token_version=td.token_version,
                            )
                    except UsersServiceUnavailableError:
                        pass
                return 200, td, {}

            body_json = json.loads(content) if content else {}
            params = params or {}

            # /categories
            if path == "/categories" and method == "GET":
                cats = service.list_categories()
                return httpx.Response(
                    200, json=[{"id": c.id, "name": c.name} for c in cats]
                )

            if path == "/categories" and method == "POST":
                code, user_data, hdrs = _get_auth_user()
                if code != 200:
                    return httpx.Response(code, json=user_data, headers=hdrs)
                if getattr(user_data, "role", "") != "admin":
                    return httpx.Response(
                        403,
                        json={
                            "detail": {
                                "code": "FORBIDDEN",
                                "message": "Admin privileges required",
                            }
                        },
                    )
                try:
                    cat_req = CategoryCreateRequest.model_validate(body_json)
                except ValidationError as exc:
                    err_list = [
                        {"msg": str(e.get("msg", "")), "loc": list(e.get("loc", []))}
                        for e in exc.errors()
                    ]
                    return httpx.Response(422, json={"detail": err_list})
                try:
                    c = service.create_category(cat_req.name)
                    return httpx.Response(201, json={"id": c.id, "name": c.name})
                except CategoryNameAlreadyExists:
                    return httpx.Response(
                        409,
                        json={
                            "detail": {
                                "code": "CATEGORY_NAME_EXISTS",
                                "message": "Already exists",
                            }
                        },
                    )

            if path.startswith("/categories/") and method == "PATCH":
                code, user_data, hdrs = _get_auth_user()
                if code != 200:
                    return httpx.Response(code, json=user_data, headers=hdrs)
                if getattr(user_data, "role", "") != "admin":
                    return httpx.Response(
                        403,
                        json={
                            "detail": {
                                "code": "FORBIDDEN",
                                "message": "Admin privileges required",
                            }
                        },
                    )
                try:
                    cat_patch = CategoryUpdateRequest.model_validate(body_json)
                except ValidationError as exc:
                    err_list = [
                        {"msg": str(e.get("msg", "")), "loc": list(e.get("loc", []))}
                        for e in exc.errors()
                    ]
                    return httpx.Response(422, json={"detail": err_list})
                cat_id = int(path.split("/")[-1])
                try:
                    c = service.update_category(cat_id, cat_patch.name)
                    return httpx.Response(200, json={"id": c.id, "name": c.name})
                except CategoryNotFound:
                    return httpx.Response(
                        404,
                        json={
                            "detail": {
                                "code": "CATEGORY_NOT_FOUND",
                                "message": "Not found",
                            }
                        },
                    )
                except CategoryNameAlreadyExists:
                    return httpx.Response(
                        409,
                        json={
                            "detail": {
                                "code": "CATEGORY_NAME_EXISTS",
                                "message": "Already exists",
                            }
                        },
                    )

            if path.startswith("/categories/") and method == "DELETE":
                code, user_data, hdrs = _get_auth_user()
                if code != 200:
                    return httpx.Response(code, json=user_data, headers=hdrs)
                if getattr(user_data, "role", "") != "admin":
                    return httpx.Response(
                        403,
                        json={
                            "detail": {
                                "code": "FORBIDDEN",
                                "message": "Admin privileges required",
                            }
                        },
                    )
                cat_id = int(path.split("/")[-1])
                try:
                    service.delete_category(cat_id)
                    return httpx.Response(204)
                except CategoryNotFound:
                    return httpx.Response(
                        404,
                        json={
                            "detail": {
                                "code": "CATEGORY_NOT_FOUND",
                                "message": "Not found",
                            }
                        },
                    )
                except CategoryInUse:
                    return httpx.Response(
                        409,
                        json={
                            "detail": {
                                "code": "CATEGORY_IN_USE",
                                "message": "In use",
                            }
                        },
                    )

            # /products
            if path == "/products" and method == "GET":
                try:
                    limit = int(params.get("limit", 20))
                    offset = int(params.get("offset", 0))
                    if limit < 1 or limit > 100 or offset < 0:
                        raise ValueError("Out of bounds")
                except (ValueError, TypeError):
                    return httpx.Response(
                        422,
                        json={
                            "detail": [{"loc": ["query"], "msg": "Invalid pagination"}]
                        },
                    )
                cat_id = (
                    int(params["category_id"])
                    if "category_id" in params and params["category_id"] is not None
                    else None
                )
                items = service.list_active_products(
                    limit=limit, offset=offset, category_id=cat_id
                )
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": p.id,
                                "seller_id": p.seller_id,
                                "category_id": p.category_id,
                                "title": p.title,
                                "price_minor": p.price_minor,
                                "currency": p.currency,
                                "stock": p.stock,
                                "active": p.active,
                            }
                            for p in items
                        ],
                        "limit": limit,
                        "offset": offset,
                    },
                )

            if path == "/products" and method == "POST":
                code, user_data, hdrs = _get_auth_user()
                if code != 200:
                    return httpx.Response(code, json=user_data, headers=hdrs)
                try:
                    prod_req = ProductCreateRequest.model_validate(body_json)
                except ValidationError as exc:
                    err_list = [
                        {"msg": str(e.get("msg", "")), "loc": list(e.get("loc", []))}
                        for e in exc.errors()
                    ]
                    return httpx.Response(422, json={"detail": err_list})
                try:
                    p = service.create_product(
                        seller_id=user_data.user_id,
                        title=prod_req.title,
                        price_minor=prod_req.price_minor,
                        currency=prod_req.currency,
                        stock=prod_req.stock,
                        active=prod_req.active,
                        category_id=prod_req.category_id,
                    )
                    return httpx.Response(
                        201,
                        json={
                            "id": p.id,
                            "seller_id": p.seller_id,
                            "category_id": p.category_id,
                            "title": p.title,
                            "price_minor": p.price_minor,
                            "currency": p.currency,
                            "stock": p.stock,
                            "active": p.active,
                        },
                    )
                except InvalidCategoryId:
                    return httpx.Response(
                        422,
                        json={
                            "detail": {
                                "code": "INVALID_CATEGORY",
                                "message": "Category not found",
                            }
                        },
                    )

            if path.startswith("/products/") and method == "GET":
                prod_id = int(path.split("/")[-1])
                try:
                    p = service.get_product_by_id(prod_id)
                    return httpx.Response(
                        200,
                        json={
                            "id": p.id,
                            "seller_id": p.seller_id,
                            "category_id": p.category_id,
                            "title": p.title,
                            "price_minor": p.price_minor,
                            "currency": p.currency,
                            "stock": p.stock,
                            "active": p.active,
                        },
                    )
                except ProductNotFound:
                    return httpx.Response(
                        404,
                        json={
                            "detail": {
                                "code": "PRODUCT_NOT_FOUND",
                                "message": "Product not found",
                            }
                        },
                    )

            if path.startswith("/products/") and method == "PATCH":
                code, user_data, hdrs = _get_auth_user()
                if code != 200:
                    return httpx.Response(code, json=user_data, headers=hdrs)
                try:
                    prod_patch = ProductUpdateRequest.model_validate(body_json)
                except ValidationError as exc:
                    err_list = [
                        {"msg": str(e.get("msg", "")), "loc": list(e.get("loc", []))}
                        for e in exc.errors()
                    ]
                    return httpx.Response(422, json={"detail": err_list})
                prod_id = int(path.split("/")[-1])
                patch_dict = prod_patch.model_dump(exclude_unset=True)
                try:
                    p = service.update_product(
                        product_id=prod_id,
                        seller_id=user_data.user_id,
                        update_data=patch_dict,
                    )
                    return httpx.Response(
                        200,
                        json={
                            "id": p.id,
                            "seller_id": p.seller_id,
                            "category_id": p.category_id,
                            "title": p.title,
                            "price_minor": p.price_minor,
                            "currency": p.currency,
                            "stock": p.stock,
                            "active": p.active,
                        },
                    )
                except ProductNotFound:
                    return httpx.Response(
                        404,
                        json={
                            "detail": {
                                "code": "PRODUCT_NOT_FOUND",
                                "message": "Product not found",
                            }
                        },
                    )
                except InvalidCategoryId:
                    return httpx.Response(
                        422,
                        json={
                            "detail": {
                                "code": "INVALID_CATEGORY",
                                "message": "Category not found",
                            }
                        },
                    )

            if path == "/users/me/products" and method == "GET":
                code, user_data, hdrs = _get_auth_user()
                if code != 200:
                    return httpx.Response(code, json=user_data, headers=hdrs)
                limit = int(params.get("limit", 20))
                offset = int(params.get("offset", 0))
                items = service.list_products_by_seller(
                    seller_id=user_data.user_id, limit=limit, offset=offset
                )
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": p.id,
                                "seller_id": p.seller_id,
                                "category_id": p.category_id,
                                "title": p.title,
                                "price_minor": p.price_minor,
                                "currency": p.currency,
                                "stock": p.stock,
                                "active": p.active,
                            }
                            for p in items
                        ],
                        "limit": limit,
                        "offset": offset,
                    },
                )

            return httpx.Response(
                404, json={"detail": {"code": "NOT_FOUND", "message": "Not found"}}
            )


@pytest.fixture(autouse=True)
def override_db_dependency() -> Generator[None]:
    """Пренасочва FastAPI зависимостта get_db_session, get_users_client и get_catalog_client към commerce_test за всички тестове."""

    def _get_test_db() -> Generator[Session]:
        with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db
    app.dependency_overrides[get_users_client] = lambda: LocalTestUsersClient(
        TestingSessionLocal
    )
    app.dependency_overrides[get_catalog_client] = lambda: LocalTestCatalogClient(
        TestingSessionLocal
    )
    yield
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_users_client, None)
    app.dependency_overrides.pop(get_catalog_client, None)
