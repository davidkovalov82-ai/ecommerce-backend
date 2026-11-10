from collections.abc import Callable
from pathlib import Path
from typing import Any

from alembic import command
from sqlalchemy.orm import Session

from app.modules.catalog.models import Product

ALEMBIC_INI_PATH = str(Path(__file__).resolve().parents[3] / "alembic.ini")


def test_migration_head_syncs_sequence_for_automatic_ids(
    temp_migration_db: Callable[..., Any],
) -> None:
    """Проверка върху отделна временна тестова БД:

    1. alembic upgrade head върху чиста база
    2. Създаване на нов продукт без ръчно подадено ID
    3. Не разчита на seed скрипта или на fixture, която коригира sequence
    """
    with temp_migration_db(
        "commerce_temp_seq_test", alembic_ini_path=ALEMBIC_INI_PATH
    ) as (alembic_cfg, temp_engine):
        # 1. Изпълнение на всички миграции до head през непривилегированата DB_USER
        command.upgrade(alembic_cfg, "head")

        # 2. Вмъкване на нов продукт без ръчно ID
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
