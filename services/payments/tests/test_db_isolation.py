"""Тестове за изолацията на PostgreSQL ролята на Payments."""

import socket
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text

from app.config import settings
from tests.conftest import TEST_DB_NAME, TEST_ROLE_NAME, TEST_ROLE_PASSWORD


def _find_closed_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_access_denied(dsn: str) -> None:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            raise AssertionError(f"Expected permission denied for {dsn}")
    except psycopg.OperationalError as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        msg = str(exc).lower()
        assert sqlstate == "42501" or "permission denied for database" in msg, (
            f"Expected explicit PostgreSQL access denial (42501), got {exc!r}"
        )


def test_payments_test_role_is_isolated_and_handles_own_ops() -> None:
    """Не-superuser ролята на payments работи в payments_test и няма достъп до чужда БД."""
    closed_port = _find_closed_local_port()
    with pytest.raises(AssertionError):
        _assert_access_denied(
            f"postgresql://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
            f"@127.0.0.1:{closed_port}/{TEST_DB_NAME}"
        )

    suffix = uuid.uuid4().hex[:8]
    peer_role = f"payments_peer_role_{suffix}"
    peer_db = f"payments_peer_db_{suffix}"
    admin_engine = create_engine(
        settings.admin_database_url, isolation_level="AUTOCOMMIT"
    )
    try:
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    f"CREATE ROLE {peer_role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD 'peer_pw'"
                )
            )
            conn.execute(text(f"CREATE DATABASE {peer_db} OWNER {peer_role}"))
            conn.execute(text(f"REVOKE ALL ON DATABASE {peer_db} FROM PUBLIC"))

        with (
            psycopg.connect(
                f"postgresql://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
                f"@{settings.db_host}:{settings.db_port}/{TEST_DB_NAME}"
            ) as own_conn,
            own_conn.cursor() as cur,
        ):
            cur.execute("SELECT current_user, pg_catalog.current_database()")
            assert cur.fetchone() == (TEST_ROLE_NAME, TEST_DB_NAME)

        _assert_access_denied(
            f"postgresql://{TEST_ROLE_NAME}:{TEST_ROLE_PASSWORD}"
            f"@{settings.db_host}:{settings.db_port}/{peer_db}"
        )
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {peer_db} WITH (FORCE)"))
            conn.execute(text(f"DROP ROLE IF EXISTS {peer_role}"))
        admin_engine.dispose()
