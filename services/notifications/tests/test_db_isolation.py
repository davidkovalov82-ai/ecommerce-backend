"""Тестове за изолация на ролите на PostgreSQL в Notifications без засягане на работни роли."""

import datetime
import uuid

import psycopg
import psycopg.errors
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import InboxEvent
from tests.conftest import TEST_DB_NAME, TEST_ROLE_NAME, TEST_ROLE_PASSWORD


def _assert_postgres_connect_access_denied(dsn: str) -> None:
    """Проверява изричен отказ за достъп (42501 / permission denied), а не срив на връзката."""
    with (
        pytest.raises(psycopg.OperationalError) as exc_info,
        psycopg.connect(dsn, connect_timeout=3),
    ):
        pass

    exc = exc_info.value
    msg = str(exc).lower()
    sqlstate = getattr(exc, "sqlstate", None)
    assert sqlstate == "42501" or "permission denied for database" in msg, (
        f"Expected PostgreSQL permission denied (42501), got connection/other error: {exc!r}"
    )


def test_notifications_test_role_is_isolated_and_handles_own_ops(
    db_session: Session,
) -> None:
    """Тестовата роля на Notifications не е superuser, работи със своята БД и получава 42501 към чужда."""
    role_flags = db_session.execute(
        text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = current_user"
        )
    ).one()
    assert tuple(role_flags) == (False, False, False)

    # 1. Собствена операция в notifications_test минава успешно
    ev = InboxEvent(
        event_id=str(uuid.uuid4()),
        event_type="order.paid",
        event_version=1,
        order_id=10,
        payload={"order_id": 10},
        occurred_at=datetime.datetime.now(datetime.UTC),
    )
    db_session.add(ev)
    db_session.commit()
    assert db_session.get(InboxEvent, ev.event_id) is not None

    # 2. Създаваме изолирана чужда БД (подобна на commerce) през админ връзката
    peer_db = "notifications_iso_peer_commerce_db"
    peer_role = "notifications_iso_peer_commerce_role"
    admin_engine = create_engine(
        settings.admin_database_url, isolation_level="AUTOCOMMIT"
    )
    peer_engine = create_engine(
        f"postgresql+psycopg://{peer_role}:peer_pass@{settings.db_host}:{settings.db_port}/{peer_db}"
    )
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {peer_db} WITH (FORCE)"))
            conn.execute(text(f"DROP ROLE IF EXISTS {peer_role}"))
            conn.execute(
                text(
                    f"CREATE ROLE {peer_role} "
                    "WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD 'peer_pass'"
                )
            )
            conn.execute(text(f"CREATE DATABASE {peer_db} OWNER {peer_role}"))
            conn.execute(text(f"REVOKE ALL ON DATABASE {peer_db} FROM PUBLIC"))
            conn.execute(
                text(f"GRANT CONNECT, TEMP ON DATABASE {peer_db} TO {peer_role}")
            )

        with peer_engine.connect() as pconn:
            pconn.execute(text("CREATE TABLE orders (id bigint PRIMARY KEY)"))
            pconn.execute(text("INSERT INTO orders VALUES (42)"))
            pconn.commit()

        # 3. Опит на Notifications ролята да се свърже към чуждата БД -> точно permission denied
        cross_dsn = (
            f"postgresql://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
            f"@{settings.db_host}:{settings.db_port}/{peer_db}"
        )
        _assert_postgres_connect_access_denied(cross_dsn)

        # 4. Проверка, че произволна мрежова грешка (затворен порт) НЕ се приема за отказ в достъпа
        bad_port_dsn = (
            f"postgresql://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
            f"@{settings.db_host}:59999/{TEST_DB_NAME}"
        )
        with pytest.raises(
            AssertionError, match="Expected PostgreSQL permission denied"
        ):
            _assert_postgres_connect_access_denied(bad_port_dsn)
    finally:
        peer_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {peer_db} WITH (FORCE)"))
            conn.execute(text(f"DROP ROLE IF EXISTS {peer_role}"))
        admin_engine.dispose()


def test_notifications_role_cannot_read_restricted_schema_inside_same_db() -> None:
    """Дори в същата БД, защитена схема връща SQLSTATE 42501 InsufficientPrivilege."""
    admin_test_url = (
        f"postgresql+psycopg://{settings.db_admin_user}:{settings.db_admin_password}"
        f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
    )
    admin_test_engine = create_engine(admin_test_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_test_engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS restricted_commerce CASCADE"))
            conn.execute(text("CREATE SCHEMA restricted_commerce"))
            conn.execute(
                text("CREATE TABLE restricted_commerce.orders (id bigint PRIMARY KEY)")
            )
            conn.execute(text("REVOKE ALL ON SCHEMA restricted_commerce FROM PUBLIC"))
            conn.execute(
                text(
                    "REVOKE ALL ON ALL TABLES IN SCHEMA restricted_commerce FROM PUBLIC"
                )
            )

        test_dsn = (
            f"postgresql://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
            f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
        )
        with psycopg.connect(test_dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM inbox_events")
            assert cur.fetchone() is not None

            with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info:
                cur.execute("SELECT * FROM restricted_commerce.orders")
            assert exc_info.value.sqlstate == "42501"
    finally:
        with admin_test_engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS restricted_commerce CASCADE"))
        admin_test_engine.dispose()
