"""Инициализира роля и база данни за Catalog през admin връзката."""

from sqlalchemy import create_engine, text

from app.config import settings


def init_db() -> None:
    """Проверява и създава потребителската роля и база данни на Catalog."""
    admin_engine = create_engine(
        settings.admin_database_url,
        isolation_level="AUTOCOMMIT",
    )
    with admin_engine.connect() as conn:
        role_exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
            {"r": settings.db_user},
        ).scalar()
        if not role_exists:
            escaped_pw = settings.db_password.replace("'", "''")
            conn.execute(
                text(
                    f'CREATE ROLE "{settings.db_user}" '
                    f"WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{escaped_pw}'"
                )
            )

        db_exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :d"),
            {"d": settings.db_name},
        ).scalar()
        if not db_exists:
            conn.execute(
                text(f'CREATE DATABASE "{settings.db_name}" OWNER "{settings.db_user}"')
            )

        conn.execute(
            text(
                f'GRANT ALL PRIVILEGES ON DATABASE "{settings.db_name}" TO "{settings.db_user}"'
            )
        )
    admin_engine.dispose()


if __name__ == "__main__":
    init_db()
