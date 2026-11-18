import os
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Генерираме изолирана RSA 2048 тестова двойка ключове за тестовата среда (само веднъж за целия процес)
if "JWT_PRIVATE_KEY" in os.environ and "JWT_PUBLIC_KEY" in os.environ:
    _test_private_pem = os.environ["JWT_PRIVATE_KEY"]
    _test_public_pem = os.environ["JWT_PUBLIC_KEY"]
else:
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
from app.modules.cart.dependencies import get_cart_client
from app.modules.catalog.dependencies import get_catalog_client
from tests.mock_models import CartItem, Product, SellerStatus, User

settings.jwt_public_key = _test_public_pem
settings.jwt_algorithm = "RS256"
settings.jwt_issuer = "ecommerce-users"
settings.jwt_audience = "ecommerce"

TEST_DB_NAME = "commerce_test"
TEST_ROLE_NAME = "commerce_test_role"
TEST_ROLE_PASSWORD = "commerce_test_role_password"
ORDERS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ALEMBIC_INI_PATH = str(ORDERS_DIR / "alembic.ini")
DEFAULT_ALEMBIC_SCRIPT_LOCATION = str(ORDERS_DIR / "migrations")
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


@pytest.fixture(scope="session", autouse=True)
def setup_test_database() -> Generator[None]:
    """Инициализира commerce_test с отделна тестова роля и реални Alembic миграции (upgrade head)."""
    main_engine = create_engine(
        settings.admin_database_url, isolation_level="AUTOCOMMIT"
    )
    escaped_pw = TEST_ROLE_PASSWORD.replace("'", "''")
    with main_engine.connect() as conn:
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
    with main_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
        conn.execute(text(f"DROP ROLE IF EXISTS {TEST_ROLE_NAME}"))
    main_engine.dispose()


def _cleanup_test_tables(session: Session) -> None:
    """Почиства всички тестови редове в Orders базата данни."""
    existing_tables = set(
        session.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).all()
    )

    for tbl in [
        "order_payment_discrepancies",
        "order_inbox_events",
        "order_outbox_events",
        "stripe_checkout_sessions",
        "stripe_processed_events",
        "payment_attempts",
        "order_items",
        "shipments",
        "orders",
    ]:
        if tbl in existing_tables:
            session.execute(text(f"DELETE FROM {tbl}"))

    if "shipments" in existing_tables:
        session.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('shipments', 'id'), "
                "coalesce(max(id), 1), max(id) IS NOT NULL) FROM shipments;"
            )
        )

    session.commit()


# ============================================================================
# MOCK CLIENTS ЗА МИКРОСЕРВИЗНИТЕ КОНТРАКТИ
# ============================================================================

from tests.mock_clients import (
    LocalTestCartClient,
    LocalTestCatalogClient,
    LocalTestUsersClient,
    shared_cart_client,
    shared_catalog_client,
    shared_users_client,
)

_orig_product_setattr = Product.__setattr__


def _hooked_product_setattr(self: Any, name: str, value: Any) -> None:
    _orig_product_setattr(self, name, value)
    if hasattr(self, "id") and self.id and self.id in shared_catalog_client._products:
        shared_catalog_client.update_product_field(self.id, name, value)


Product.__setattr__ = _hooked_product_setattr  # type: ignore[method-assign]


# ============================================================================
# MOCK ЗАЯВКИ ЗА СТАРИТЕ ТЕСТОВЕ (INTERCEPTORS)
# ============================================================================


class MockCartItemQuery:
    def __init__(
        self, cart_client: LocalTestCartClient, user_id: int | None = None
    ) -> None:
        self.cart_client = cart_client
        self.user_id = user_id

    def filter(self, *criteria: Any) -> "MockCartItemQuery":
        user_id = self.user_id
        for crit in criteria:
            if hasattr(crit, "right") and hasattr(crit.right, "value"):
                user_id = crit.right.value
            elif (
                hasattr(crit, "right")
                and hasattr(crit.right, "element")
                and hasattr(crit.right.element, "value")
            ):
                user_id = crit.right.element.value
        return MockCartItemQuery(self.cart_client, user_id=user_id)

    def filter_by(self, **kwargs: Any) -> "MockCartItemQuery":
        user_id = kwargs.get("user_id", self.user_id)
        return MockCartItemQuery(self.cart_client, user_id=user_id)

    def _get_items(self) -> list[CartItem]:
        items: list[CartItem] = []
        if self.user_id is not None:
            user_cart = self.cart_client._carts.get(self.user_id, {})
            for pid, cdata in user_cart.items():
                items.append(
                    CartItem(
                        user_id=self.user_id,
                        product_id=pid,
                        quantity=cdata["quantity"],
                    )
                )
        else:
            for uid, user_cart in self.cart_client._carts.items():
                for pid, cdata in user_cart.items():
                    items.append(
                        CartItem(
                            user_id=uid,
                            product_id=pid,
                            quantity=cdata["quantity"],
                        )
                    )
        return items

    def all(self) -> list[CartItem]:
        return self._get_items()

    def count(self) -> int:
        return len(self._get_items())

    def first(self) -> CartItem | None:
        items = self._get_items()
        return items[0] if items else None


class MockUserQuery:
    def __init__(
        self,
        users_client: LocalTestUsersClient,
        user_id: int | None = None,
        email: str | None = None,
    ) -> None:
        self.users_client = users_client
        self.user_id = user_id
        self.email = email

    def filter_by(self, **kwargs: Any) -> "MockUserQuery":
        return MockUserQuery(
            self.users_client,
            user_id=kwargs.get("id", self.user_id),
            email=kwargs.get("email", self.email),
        )

    def filter(self, *criteria: Any) -> "MockUserQuery":
        user_id = self.user_id
        email = self.email
        for crit in criteria:
            if hasattr(crit, "left") and hasattr(crit, "right"):
                col = getattr(crit.left, "name", "")
                val = getattr(
                    crit.right,
                    "value",
                    getattr(getattr(crit.right, "element", None), "value", None),
                )
                if col == "id":
                    user_id = val
                elif col == "email":
                    email = val
        return MockUserQuery(self.users_client, user_id=user_id, email=email)

    def _get_users(self) -> list[User]:
        users: list[User] = []
        for u in self.users_client._users.values():
            if self.user_id is not None and u.id != self.user_id:
                continue
            if self.email is not None and u.email != self.email:
                continue
            users.append(
                User(
                    id=u.id,
                    email=u.email,
                    role=u.role,
                    status=u.status,
                    token_version=u.token_version,
                    display_name=u.display_name,
                )
            )
        return users

    def all(self) -> list[User]:
        return self._get_users()

    def count(self) -> int:
        return len(self._get_users())

    def first(self) -> User | None:
        users = self._get_users()
        return users[0] if users else None


class MockProductQuery:
    def __init__(
        self,
        catalog_client: LocalTestCatalogClient,
        product_id: int | None = None,
    ) -> None:
        self.catalog_client = catalog_client
        self.product_id = product_id

    def filter_by(self, **kwargs: Any) -> "MockProductQuery":
        return MockProductQuery(
            self.catalog_client,
            product_id=kwargs.get("id", self.product_id),
        )

    def filter(self, *criteria: Any) -> "MockProductQuery":
        product_id = self.product_id
        for crit in criteria:
            if hasattr(crit, "left") and hasattr(crit, "right"):
                col = getattr(crit.left, "name", "")
                val = getattr(
                    crit.right,
                    "value",
                    getattr(getattr(crit.right, "element", None), "value", None),
                )
                if col == "id":
                    product_id = val
        return MockProductQuery(self.catalog_client, product_id=product_id)

    def _get_products(self) -> list[Product]:
        products: list[Product] = []
        for p in self.catalog_client._products.values():
            if self.product_id is not None and p.id != self.product_id:
                continue
            products.append(
                Product(
                    id=p.id,
                    seller_id=p.seller_id,
                    category_id=p.category_id,
                    title=p.title,
                    price_minor=p.price_minor,
                    currency=p.currency,
                    stock=p.stock,
                    active=p.active,
                )
            )
        return products

    def all(self) -> list[Product]:
        return self._get_products()

    def count(self) -> int:
        return len(self._get_products())

    def first(self) -> Product | None:
        products = self._get_products()
        return products[0] if products else None


_orig_session_get = Session.get
_orig_session_add = Session.add
_orig_session_query = Session.query
_orig_session_refresh = Session.refresh


def _hooked_session_get(self: Session, entity: Any, ident: Any, **kwargs: Any) -> Any:
    entity_name = getattr(entity, "__name__", "")
    if entity is Product or entity_name == "Product":
        p_info = shared_catalog_client.get_product(ident)
        if p_info is None:
            return None
        return Product(
            id=p_info.id,
            seller_id=p_info.seller_id,
            category_id=p_info.category_id,
            title=p_info.title,
            price_minor=p_info.price_minor,
            currency=p_info.currency,
            stock=p_info.stock,
            active=p_info.active,
        )
    if entity is User or entity_name == "User":
        u_info = shared_users_client.get_user_status(ident)
        if u_info is None:
            return None
        return User(
            id=u_info.id,
            email=u_info.email,
            role=u_info.role,
            status=u_info.status,
            token_version=u_info.token_version,
            display_name=u_info.display_name,
        )
    if entity is CartItem or entity_name == "CartItem":
        return None
    return _orig_session_get(self, entity, ident, **kwargs)


_mock_id_seq = 1000


def _next_mock_id() -> int:
    global _mock_id_seq
    _mock_id_seq += 1
    return _mock_id_seq


def _hooked_session_add(self: Session, instance: Any, **kwargs: Any) -> None:
    if isinstance(instance, Product):
        if getattr(instance, "id", None) is None:
            instance.id = _next_mock_id()
        instance.seller_id = (
            instance.seller_id
            if getattr(instance, "seller_id", None) is not None
            else 1
        )
        instance.currency = getattr(instance, "currency", None) or "EUR"
        instance.stock = (
            instance.stock if getattr(instance, "stock", None) is not None else 0
        )
        instance.active = (
            instance.active if getattr(instance, "active", None) is not None else True
        )
        shared_catalog_client.set_mock_product(
            product_id=instance.id,
            seller_id=instance.seller_id,
            category_id=getattr(instance, "category_id", None),
            title=instance.title,
            price_minor=instance.price_minor,
            currency=instance.currency,
            stock=instance.stock,
            active=instance.active,
        )
        return
    if isinstance(instance, User):
        if getattr(instance, "id", None) is None:
            instance.id = _next_mock_id()
        instance.token_version = (
            instance.token_version
            if getattr(instance, "token_version", None) is not None
            else 0
        )
        instance.status = getattr(instance, "status", None) or "active"
        shared_users_client.set_mock_user(
            user_id=instance.id,
            email=instance.email,
            role=instance.role or "customer",
            status=instance.status,
            token_version=instance.token_version,
            display_name=getattr(instance, "display_name", None),
        )
        return
    if isinstance(instance, CartItem):
        shared_cart_client.set_item(
            user_id=instance.user_id,
            product_id=instance.product_id,
            quantity=instance.quantity,
            version=getattr(instance, "version", 1),
        )
        return
    if isinstance(instance, SellerStatus):
        return
    return _orig_session_add(self, instance, **kwargs)


def _hooked_session_query(self: Session, *entities: Any, **kwargs: Any) -> Any:
    if entities:
        first = entities[0]
        first_name = getattr(first, "__name__", "")
        if first is CartItem or first_name == "CartItem":
            return MockCartItemQuery(shared_cart_client)
        if first is User or first_name == "User":
            return MockUserQuery(shared_users_client)
        if first is Product or first_name == "Product":
            return MockProductQuery(shared_catalog_client)
    return _orig_session_query(self, *entities, **kwargs)


def _hooked_session_refresh(self: Session, instance: Any, **kwargs: Any) -> None:
    if isinstance(instance, Product):
        p_info = shared_catalog_client.get_product(instance.id)
        if p_info:
            instance.title = p_info.title
            instance.price_minor = p_info.price_minor
            instance.stock = p_info.stock
            instance.active = p_info.active
        return
    if isinstance(instance, User):
        u_info = shared_users_client.get_user_status(instance.id)
        if u_info:
            instance.email = u_info.email
            instance.role = u_info.role
            instance.status = u_info.status
        return
    if isinstance(instance, CartItem):
        return
    return _orig_session_refresh(self, instance, **kwargs)


Session.get = _hooked_session_get  # type: ignore[assignment]
Session.add = _hooked_session_add  # type: ignore[assignment]
Session.query = _hooked_session_query  # type: ignore[assignment]
Session.refresh = _hooked_session_refresh  # type: ignore[assignment]


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
def clean_test_state() -> Generator[None]:
    """Гарантира почистване на базата и mock клиентите преди и след всеки тест."""
    with TestingSessionLocal() as session:
        _cleanup_test_tables(session)
    shared_users_client._users.clear()
    shared_catalog_client._products.clear()
    shared_catalog_client._reservations.clear()
    shared_catalog_client._released_orders.clear()
    shared_cart_client._carts.clear()
    shared_cart_client._cleared_orders.clear()

    yield

    with TestingSessionLocal() as session:
        _cleanup_test_tables(session)
    shared_users_client._users.clear()
    shared_catalog_client._products.clear()
    shared_catalog_client._reservations.clear()
    shared_catalog_client._released_orders.clear()
    shared_cart_client._carts.clear()
    shared_cart_client._cleared_orders.clear()


@pytest.fixture(autouse=True)
def override_db_dependency() -> Generator[None]:
    """Пренасочва зависимостите към тестовата база и mock клиентите."""

    def _get_test_db() -> Generator[Session]:
        with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db
    app.dependency_overrides[get_users_client] = lambda: shared_users_client
    app.dependency_overrides[get_catalog_client] = lambda: shared_catalog_client
    app.dependency_overrides[get_cart_client] = lambda: shared_cart_client
    yield
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_users_client, None)
    app.dependency_overrides.pop(get_catalog_client, None)
    app.dependency_overrides.pop(get_cart_client, None)
