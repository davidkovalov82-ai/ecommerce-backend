"""Миграционни тестове за Payments базата данни (0001 -> 0002 и безопасен downgrade)."""

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.config import settings

PAYMENTS_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI_PATH = str(PAYMENTS_DIR / "alembic.ini")
ALEMBIC_SCRIPT_LOCATION = str(PAYMENTS_DIR / "migrations")


def test_payments_0002_closures_discrepancies_upgrade_and_safe_downgrade() -> None:
    """Миграция 0002 добавя payment_order_closures и payment_discrepancies и пази диагностиката при downgrade."""
    temp_db = "payments_temp_mig_0002_test"
    temp_role = "payments_temp_mig_role"
    temp_pw = "payments_temp_mig_pw"

    admin_engine = create_engine(
        settings.admin_database_url, isolation_level="AUTOCOMMIT"
    )
    quoted_pw = temp_pw.replace("'", "''")
    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db} WITH (FORCE)"))
        conn.execute(text(f"DROP ROLE IF EXISTS {temp_role}"))
        conn.execute(
            text(
                f"CREATE ROLE {temp_role} "
                f"WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{quoted_pw}'"
            )
        )
        conn.execute(text(f"CREATE DATABASE {temp_db} OWNER {temp_role}"))
        conn.execute(text(f"REVOKE ALL ON DATABASE {temp_db} FROM PUBLIC"))
        conn.execute(text(f"GRANT CONNECT, TEMP ON DATABASE {temp_db} TO {temp_role}"))

    unpriv_url_obj = URL.create(
        drivername="postgresql+psycopg",
        username=temp_role,
        password=temp_pw,
        host=settings.db_host,
        port=settings.db_port,
        database=temp_db,
    )
    unpriv_url = unpriv_url_obj.render_as_string(hide_password=False)
    temp_engine = create_engine(unpriv_url)

    try:
        cfg = Config(ALEMBIC_INI_PATH)
        cfg.set_main_option("script_location", ALEMBIC_SCRIPT_LOCATION)
        cfg.set_main_option("sqlalchemy.url", unpriv_url.replace("%", "%%"))

        command.upgrade(cfg, "0001")
        command.upgrade(cfg, "0002")

        with Session(temp_engine) as session:
            session.execute(
                text(
                    "INSERT INTO payment_order_closures (order_id, status, reason) "
                    "VALUES (10, 'cancelled', 'cancel')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO payment_discrepancies "
                    "(order_id, payment_attempt_id, source, reason, details) "
                    "VALUES (10, 1, 'webhook', 'amount_mismatch', '{}'::json)"
                )
            )
            session.commit()

        try:
            command.downgrade(cfg, "0001")
            raise AssertionError(
                "Expected RuntimeError when payment_discrepancies exist"
            )
        except RuntimeError as exc:
            assert "payment discrepancies" in str(exc)

        with Session(temp_engine) as session:
            session.execute(text("DELETE FROM payment_discrepancies"))
            session.execute(text("DELETE FROM payment_order_closures"))
            session.commit()

        command.downgrade(cfg, "0001")

        with Session(temp_engine) as session:
            closures_exists = session.execute(
                text("SELECT to_regclass('public.payment_order_closures') IS NOT NULL")
            ).scalar()
            disc_exists = session.execute(
                text("SELECT to_regclass('public.payment_discrepancies') IS NOT NULL")
            ).scalar()
            assert closures_exists is False
            assert disc_exists is False
    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db} WITH (FORCE)"))
            conn.execute(text(f"DROP ROLE IF EXISTS {temp_role}"))
        admin_engine.dispose()


def _unused_placeholder(_arg: Any = None) -> None:
    return None
