"""Тестове за изолация на ролите на PostgreSQL между Commerce и чужди бази данни."""

import psycopg
import psycopg.errors
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings
from tests.conftest import TEST_DB_NAME, TEST_ROLE_NAME, TEST_ROLE_PASSWORD


def _assert_postgres_connect_access_denied(dsn: str) -> None:
    """Проверява изрично отказ в достъпа (42501 / permission denied), а не произволна мрежова грешка."""
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


def test_commerce_test_role_cannot_read_peer_database_and_handles_own_ops(
    db_session: Session,
) -> None:
    """Ролята на Commerce не е superuser, изпълнява своите заявки и получава 42501 към чужда БД."""
    # 1. Проверяваме, че тестовата роля НЕ е superuser и има достъп до своята БД
    role_flags = db_session.execute(
        text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = current_user"
        )
    ).one()
    assert tuple(role_flags) == (False, False, False)

    # 2. Създаваме изолирана чужда БД и таблица през админ връзката
    peer_db = "commerce_iso_peer_notifications_db"
    peer_role = "commerce_iso_peer_notifications_role"
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
            pconn.execute(text("CREATE TABLE inbox_events (event_id text PRIMARY KEY)"))
            pconn.execute(text("INSERT INTO inbox_events VALUES ('evt-secret')"))
            pconn.commit()

        # 3. Опит на Commerce ролята да се свърже с чуждата БД -> точно permission denied
        cross_dsn = (
            f"postgresql://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
            f"@{settings.db_host}:{settings.db_port}/{peer_db}"
        )
        _assert_postgres_connect_access_denied(cross_dsn)

        # 4. Доказателство, че хелпърът НЕ приема произволна грешка на връзката (напр. затворен порт)
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


def test_commerce_role_cannot_select_from_restricted_schema_inside_same_db() -> None:
    """Дори в рамките на същата БД, таблица в защитена схема хвърля InsufficientPrivilege (42501)."""
    admin_engine = create_engine(
        settings.admin_database_url, isolation_level="AUTOCOMMIT"
    )
    admin_on_test_url = (
        f"postgresql+psycopg://{settings.db_admin_user or settings.db_user}:"
        f"{settings.db_admin_password if settings.db_admin_password is not None else settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
    )
    admin_test_engine = create_engine(admin_on_test_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_test_engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS restricted_notif CASCADE"))
            conn.execute(text("CREATE SCHEMA restricted_notif"))
            conn.execute(
                text(
                    "CREATE TABLE restricted_notif.inbox_events (event_id text PRIMARY KEY)"
                )
            )
            conn.execute(text("REVOKE ALL ON SCHEMA restricted_notif FROM PUBLIC"))
            conn.execute(
                text("REVOKE ALL ON ALL TABLES IN SCHEMA restricted_notif FROM PUBLIC")
            )

        test_dsn = (
            f"postgresql://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
            f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
        )
        with psycopg.connect(test_dsn) as conn, conn.cursor() as cur:
            # Собствената таблица на Commerce работи
            cur.execute("SELECT COUNT(*) FROM order_outbox_events")
            assert cur.fetchone() is not None

            # Защитената таблица дава точно 42501 InsufficientPrivilege
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info:
                cur.execute("SELECT * FROM restricted_notif.inbox_events")
            assert exc_info.value.sqlstate == "42501"
    finally:
        with admin_test_engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS restricted_notif CASCADE"))
        admin_test_engine.dispose()
        admin_engine.dispose()
