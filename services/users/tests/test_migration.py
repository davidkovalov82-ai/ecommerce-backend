"""Тестове за прилагане на Alembic миграции върху чиста изолирана база данни."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import settings

TEMP_MIG_DB_NAME = "users_mig_temp_test"
TEMP_MIG_USER = "users_mig_temp_role"
TEMP_MIG_PASSWORD = "users_mig_temp_password"


def test_migration_upgrade_and_downgrade_on_empty_db() -> None:
    """Проверява, че първоначалната миграция се прилага успешно към празна база и се връща обратно."""
    admin_engine = create_engine(
        settings.admin_database_url,
        isolation_level="AUTOCOMMIT",
    )
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEMP_MIG_DB_NAME}" WITH (FORCE)'))
        conn.execute(text(f'DROP ROLE IF EXISTS "{TEMP_MIG_USER}"'))
        conn.execute(
            text(
                f'CREATE ROLE "{TEMP_MIG_USER}" '
                f"WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{TEMP_MIG_PASSWORD}'"
            )
        )
        conn.execute(
            text(f'CREATE DATABASE "{TEMP_MIG_DB_NAME}" OWNER "{TEMP_MIG_USER}"')
        )
        conn.execute(text(f'REVOKE ALL ON DATABASE "{TEMP_MIG_DB_NAME}" FROM PUBLIC'))
        conn.execute(
            text(
                f'GRANT CONNECT, TEMP ON DATABASE "{TEMP_MIG_DB_NAME}" TO "{TEMP_MIG_USER}"'
            )
        )

    temp_db_url = (
        f"postgresql+psycopg://{TEMP_MIG_USER}:{TEMP_MIG_PASSWORD}"
        f"@{settings.db_host}:{settings.db_port}/{TEMP_MIG_DB_NAME}"
    )

    users_dir = Path(__file__).resolve().parent.parent
    alembic_ini_path = str(users_dir / "alembic.ini")
    alembic_cfg = Config(alembic_ini_path)
    alembic_cfg.set_main_option("script_location", str(users_dir / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", temp_db_url)

    engine = create_engine(temp_db_url)
    try:
        # Прилагане на миграциите
        command.upgrade(alembic_cfg, "head")

        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        assert "users" in table_names
        assert "password_reset_tokens" in table_names
        assert "account_moderation_logs" in table_names

        columns = {c["name"]: c for c in inspector.get_columns("users")}
        assert "id" in columns
        assert "email" in columns
        assert "password_hash" in columns
        assert "role" in columns
        assert "status" in columns
        assert "display_name" in columns
        assert "token_version" in columns

        # Връщане на миграцията (downgrade)
        command.downgrade(alembic_cfg, "base")
        inspector_after = inspect(engine)
        assert "users" not in inspector_after.get_table_names()
        assert "password_reset_tokens" not in inspector_after.get_table_names()
        assert "account_moderation_logs" not in inspector_after.get_table_names()
    finally:
        engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(
                text(f'DROP DATABASE IF EXISTS "{TEMP_MIG_DB_NAME}" WITH (FORCE)')
            )
            conn.execute(text(f'DROP ROLE IF EXISTS "{TEMP_MIG_USER}"'))
        admin_engine.dispose()
