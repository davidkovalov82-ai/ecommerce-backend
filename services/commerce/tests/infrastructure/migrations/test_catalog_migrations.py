from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.catalog.models import Product

ALEMBIC_INI_PATH = str(Path(__file__).resolve().parents[3] / "alembic.ini")


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
        alembic_cfg = Config(ALEMBIC_INI_PATH)
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
