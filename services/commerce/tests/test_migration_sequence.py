from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.catalog.models import Product


def test_migration_head_syncs_sequence_for_automatic_ids() -> None:
    """Проверка върху отделна временна тестова БД:

    1. alembic upgrade head върху чиста база
    2. Създаване на нов продукт без ръчно подадено ID
    3. Не разчита на seed скрипта или на fixture, която коригира sequence
    """
    temp_db_name = "commerce_temp_seq_test"
    admin_engine = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")

    # 1. Създаване на чиста временна база данни
    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {temp_db_name}"))

    temp_db_url = (
        f"postgresql+psycopg://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{temp_db_name}"
    )

    temp_engine = create_engine(temp_db_url)
    try:
        # 2. Изпълнение на всички миграции до head
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", temp_db_url)
        command.upgrade(alembic_cfg, "head")

        # 3. Вмъкване на нов продукт без ръчно ID
        with Session(temp_engine) as session:
            # Първоначалният Notebook с id=1 трябва да е вмъкнат от миграцията
            initial_product = session.get(Product, 1)
            assert initial_product is not None
            assert initial_product.title == "Notebook"

            # Вмъкваме втори продукт БЕЗ ръчно id
            new_product = Product(
                title="Autoincremented Product",
                price_minor=4999,
                currency="EUR",
                stock=15,
                active=True,
            )
            session.add(new_product)
            session.commit()
            session.refresh(new_product)

            # Проверяваме, че id е генерирано автоматично като 2, без конфликт
            assert new_product.id == 2
    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        admin_engine.dispose()


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
        alembic_cfg = Config("alembic.ini")
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

        # 3. Обновяване до head (orders и order_items)
        command.upgrade(alembic_cfg, "head")

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
