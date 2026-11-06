from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings

ALEMBIC_INI_PATH = str(Path(__file__).resolve().parents[3] / "alembic.ini")


def test_orders_migration_upgrade_with_existing_data() -> None:
    """Проверка за обновяване от предишната ревизия с налични данни и безопасно връщане назад."""
    temp_db_name = "commerce_temp_orders_mig_test"
    admin_engine = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {temp_db_name}"))

    temp_db_url = (
        f"postgresql+psycopg://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{temp_db_name}"
    )

    temp_engine = create_engine(temp_db_url)
    try:
        alembic_cfg = Config(ALEMBIC_INI_PATH)
        alembic_cfg.set_main_option("sqlalchemy.url", temp_db_url)

        # 1. Миграция до cart_items (9c17d651dd68)
        command.upgrade(alembic_cfg, "9c17d651dd68")

        # 2. Добавяне на тестови данни в users, products и cart_items
        with Session(temp_engine) as session:
            session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) "
                    "VALUES (100, 'test_user@example.com', 'dummy_hash', 'customer')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO cart_items (user_id, product_id, quantity) "
                    "VALUES (100, 1, 2)"
                )
            )
            session.commit()

        # 3. Обновяване до 568b759ac3a0 (orders и order_items)
        command.upgrade(alembic_cfg, "568b759ac3a0")

        # 4. Проверка, че съществуващите данни са непокътнати и новите таблици са налични
        with Session(temp_engine) as session:
            res_user = session.execute(
                text("SELECT email FROM users WHERE id = 100")
            ).scalar()
            assert res_user == "test_user@example.com"

            res_cart = session.execute(
                text(
                    "SELECT quantity FROM cart_items WHERE user_id = 100 AND product_id = 1"
                )
            ).scalar()
            assert res_cart == 2

            # Таблица orders съществува и е празна
            orders_count = session.execute(text("SELECT count(*) FROM orders")).scalar()
            assert orders_count == 0

        # 5. Връщане назад (downgrade) до 9c17d651dd68
        command.downgrade(alembic_cfg, "9c17d651dd68")

        with Session(temp_engine) as session:
            # orders и order_items са премахнати, а cart_items остава
            res_cart_after = session.execute(
                text(
                    "SELECT quantity FROM cart_items WHERE user_id = 100 AND product_id = 1"
                )
            ).scalar()
            assert res_cart_after == 2
    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        admin_engine.dispose()


def test_outbox_migration_upgrade_and_downgrade() -> None:
    """Тества миграция bd4c339e0dfa: създаване на order_outbox_events и безопасно връщане назад."""
    temp_db_name = "commerce_temp_outbox_mig_test"
    admin_engine = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {temp_db_name}"))

    temp_db_url = (
        f"postgresql+psycopg://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{temp_db_name}"
    )

    temp_engine = create_engine(temp_db_url)
    try:
        alembic_cfg = Config(ALEMBIC_INI_PATH)
        alembic_cfg.set_main_option("sqlalchemy.url", temp_db_url)

        # 1. Миграция до b4f8c2e1a9d0
        command.upgrade(alembic_cfg, "b4f8c2e1a9d0")

        with Session(temp_engine) as session:
            session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) "
                    "VALUES (800, 'outbox_user@example.com', 'dummy_hash', 'customer')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO orders (id, user_id, status, currency, total_minor, idempotency_key) "
                    "VALUES (800, 800, 'paid', 'EUR', 2000, 'idemp-outbox-mig')"
                )
            )
            session.commit()

        # 2. Upgrade до bd4c339e0dfa (създаване на order_outbox_events)
        command.upgrade(alembic_cfg, "bd4c339e0dfa")

        with Session(temp_engine) as session:
            table_exists = session.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM information_schema.tables "
                    "  WHERE table_name = 'order_outbox_events'"
                    ")"
                )
            ).scalar()
            assert table_exists is True

            # Вмъкване на събитие
            session.execute(
                text(
                    "INSERT INTO order_outbox_events (order_id, event_type, payload, status) "
                    "VALUES (800, 'order.paid', '{\"order_id\": 800}', 'pending')"
                )
            )
            session.commit()

        # 3. Downgrade до b4f8c2e1a9d0
        command.downgrade(alembic_cfg, "b4f8c2e1a9d0")

        with Session(temp_engine) as session:
            table_dropped = session.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM information_schema.tables "
                    "  WHERE table_name = 'order_outbox_events'"
                    ")"
                )
            ).scalar()
            assert table_dropped is False

            # Поръчката остава запазена
            order_rem = session.execute(
                text("SELECT id FROM orders WHERE id = 800")
            ).scalar()
            assert order_rem == 800
    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        admin_engine.dispose()
