from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
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


def test_payments_migration_upgrade_with_existing_orders() -> None:
    """Проверка за обновяване от 568b759ac3a0 с вече съществуваща поръчка и безопасно връщане назад."""
    temp_db_name = "commerce_temp_payments_mig_test"
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

        # 1. Миграция до 568b759ac3a0 (създаване на orders с pending_payment CHECK)
        command.upgrade(alembic_cfg, "568b759ac3a0")

        # 2. Вмъкване на потребител и съществуваща поръчка с pending_payment
        with Session(temp_engine) as session:
            session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) "
                    "VALUES (200, 'buyer@example.com', 'dummy_hash', 'customer')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO orders (id, user_id, status, currency, total_minor, idempotency_key) "
                    "VALUES (500, 200, 'pending_payment', 'EUR', 3500, 'existing-key-1')"
                )
            )
            session.commit()

        # 3. Обновяване до head (разширяване на статуса и таблица payment_attempts)
        command.upgrade(alembic_cfg, "head")

        # 4. Проверка: съществуващата поръчка е непокътната и може да стане 'paid'
        with Session(temp_engine) as session:
            status = session.execute(
                text("SELECT status FROM orders WHERE id = 500")
            ).scalar()
            assert status == "pending_payment"

            # Можем да променим статуса на 'paid' благодарение на разширеното ограничение
            session.execute(text("UPDATE orders SET status = 'paid' WHERE id = 500"))
            session.commit()

            new_status = session.execute(
                text("SELECT status FROM orders WHERE id = 500")
            ).scalar()
            assert new_status == "paid"

            # Таблица payment_attempts съществува и записва опит
            session.execute(
                text(
                    "INSERT INTO payment_attempts (id, order_id, payment_method, status, amount_minor, currency, idempotency_key) "
                    "VALUES (1, 500, 'test_success', 'succeeded', 3500, 'EUR', 'pay-key-1')"
                )
            )
            session.commit()

            # Втори успешен опит за същата поръчка трябва да гръмне заради частичния уникален индекс
            try:
                session.execute(
                    text(
                        "INSERT INTO payment_attempts (id, order_id, payment_method, status, amount_minor, currency, idempotency_key) "
                        "VALUES (2, 500, 'test_success', 'succeeded', 3500, 'EUR', 'pay-key-2')"
                    )
                )
                session.commit()
                assert False, "Очакваше се грешка за втори succeeded опит"
            except IntegrityError:
                session.rollback()

        # Връщаме статуса на pending_payment и почистваме payment_attempts за чист downgrade
        with Session(temp_engine) as session:
            session.execute(text("DELETE FROM payment_attempts"))
            session.execute(
                text("UPDATE orders SET status = 'pending_payment' WHERE id = 500")
            )
            session.commit()

        # 5. Downgrade обратно до 568b759ac3a0
        command.downgrade(alembic_cfg, "568b759ac3a0")

        with Session(temp_engine) as session:
            # payment_attempts е премахната
            table_exists = session.execute(
                text(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'payment_attempts')"
                )
            ).scalar()
            assert table_exists is False
            # Съществуващата поръчка остава запазена
            rem_status = session.execute(
                text("SELECT status FROM orders WHERE id = 500")
            ).scalar()
            assert rem_status == "pending_payment"
    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        admin_engine.dispose()


def test_stripe_migration_upgrade_and_downgrade() -> None:
    """Проверка за обновяване от c74a9d821e5f до e2b83a741f90 и безопасно връщане назад."""
    temp_db_name = "commerce_temp_stripe_mig_test"
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

        # 1. Миграция до c74a9d821e5f (преди Stripe таблиците)
        command.upgrade(alembic_cfg, "c74a9d821e5f")

        # 2. Вмъкване на поръчка
        with Session(temp_engine) as session:
            session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) "
                    "VALUES (300, 'stripe_user@example.com', 'dummy_hash', 'customer')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO orders (id, user_id, status, currency, total_minor, idempotency_key) "
                    "VALUES (600, 300, 'pending_payment', 'EUR', 4500, 'order-key-300')"
                )
            )
            session.commit()

        # 3. Upgrade до head (e2b83a741f90)
        command.upgrade(alembic_cfg, "e2b83a741f90")

        with Session(temp_engine) as session:
            # Проверяваме съществуването на новите таблици
            t1 = session.execute(
                text(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'stripe_checkout_sessions')"
                )
            ).scalar()
            t2 = session.execute(
                text(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'stripe_processed_events')"
                )
            ).scalar()
            assert t1 is True
            assert t2 is True

            # Вмъкваме запис в stripe_checkout_sessions
            session.execute(
                text(
                    "INSERT INTO stripe_checkout_sessions (id, order_id, status, amount_minor, currency, idempotency_key) "
                    "VALUES (1, 600, 'open', 4500, 'EUR', 'stripe-idemp-1')"
                )
            )
            # Вмъкваме събитие в stripe_processed_events
            session.execute(
                text(
                    "INSERT INTO stripe_processed_events (stripe_event_id, event_type) "
                    "VALUES ('evt_test_1', 'checkout.session.completed')"
                )
            )
            session.commit()

            # Проверка за уникалност на stripe_event_id
            try:
                session.execute(
                    text(
                        "INSERT INTO stripe_processed_events (stripe_event_id, event_type) "
                        "VALUES ('evt_test_1', 'checkout.session.completed')"
                    )
                )
                session.commit()
                assert False, "Очакваше се IntegrityError за дублиран event_id"
            except IntegrityError:
                session.rollback()

        # Почистваме преди downgrade
        with Session(temp_engine) as session:
            session.execute(text("DELETE FROM stripe_checkout_sessions"))
            session.execute(text("DELETE FROM stripe_processed_events"))
            session.commit()

        # 4. Downgrade до c74a9d821e5f
        command.downgrade(alembic_cfg, "c74a9d821e5f")

        with Session(temp_engine) as session:
            t1_after = session.execute(
                text(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'stripe_checkout_sessions')"
                )
            ).scalar()
            t2_after = session.execute(
                text(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'stripe_processed_events')"
                )
            ).scalar()
            assert t1_after is False
            assert t2_after is False

            # Поръчката остава запазена
            order_rem = session.execute(
                text("SELECT id FROM orders WHERE id = 600")
            ).scalar()
            assert order_rem == 600
    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        admin_engine.dispose()


def test_order_processing_and_stripe_mode_migration() -> None:
    """Тества миграция b4f8c2e1a9d0:

    - Добавяне на expires_at в orders
    - Разширяване на валидните статуси на orders (payment_processing, expired)
    - Добавяне на колона mode в stripe_checkout_sessions (по подразбиране 'test')
    - Разширяване на валидните статуси на stripe_checkout_sessions (processing)
    - Ограничение за mode ('test', 'live')
    - Безопасен downgrade назад до e2b83a741f90
    """
    temp_db_name = "commerce_temp_mode_mig_test"
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

        # 1. Миграция до предишната ревизия e2b83a741f90
        command.upgrade(alembic_cfg, "e2b83a741f90")

        with Session(temp_engine) as session:
            session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) "
                    "VALUES (700, 'mode_user@example.com', 'dummy_hash', 'customer')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO orders (id, user_id, status, total_minor, currency, idempotency_key) "
                    "VALUES (700, 700, 'pending_payment', 5000, 'EUR', 'idemp_order_700')"
                )
            )
            # Вмъкваме сесия без колона mode (тя все още не съществува)
            session.execute(
                text(
                    "INSERT INTO stripe_checkout_sessions (id, order_id, status, amount_minor, currency, idempotency_key) "
                    "VALUES (700, 700, 'open', 5000, 'EUR', 'idemp-old-700')"
                )
            )
            session.commit()

        # 2. Upgrade до b4f8c2e1a9d0
        command.upgrade(alembic_cfg, "b4f8c2e1a9d0")

        with Session(temp_engine) as session:
            # Проверяваме, че старата сесия е получила mode = 'test'
            existing_mode = session.execute(
                text("SELECT mode FROM stripe_checkout_sessions WHERE id = 700")
            ).scalar()
            assert existing_mode == "test"

            # Проверяваме новите статуси на поръчки: payment_processing и expired
            session.execute(
                text("UPDATE orders SET status = 'payment_processing' WHERE id = 700")
            )
            session.commit()

            session.execute(text("UPDATE orders SET status = 'expired' WHERE id = 700"))
            session.commit()

            # Проверяваме новия статус на сесия: processing
            session.execute(
                text(
                    "UPDATE stripe_checkout_sessions SET status = 'processing' WHERE id = 700"
                )
            )
            session.commit()

            # Проверяваме ограничението за mode (само test или live)
            try:
                session.execute(
                    text(
                        "INSERT INTO stripe_checkout_sessions (id, order_id, status, amount_minor, currency, idempotency_key, mode) "
                        "VALUES (701, 700, 'open', 5000, 'EUR', 'idemp-inv-mode', 'invalid')"
                    )
                )
                session.commit()
                assert False, "Очакваше се IntegrityError за невалиден mode"
            except IntegrityError:
                session.rollback()

        # Почистваме преди downgrade
        with Session(temp_engine) as session:
            session.execute(text("DELETE FROM stripe_checkout_sessions"))
            session.execute(
                text("UPDATE orders SET status = 'pending_payment' WHERE id = 700")
            )
            session.commit()

        # 3. Downgrade обратно до e2b83a741f90
        command.downgrade(alembic_cfg, "e2b83a741f90")

        with Session(temp_engine) as session:
            # Проверяваме, че колоната mode вече не съществува в stripe_checkout_sessions
            col_mode_exists = session.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM information_schema.columns "
                    "  WHERE table_name = 'stripe_checkout_sessions' AND column_name = 'mode'"
                    ")"
                )
            ).scalar()
            assert col_mode_exists is False

            # Проверяваме, че колоната expires_at вече не съществува в orders
            col_exp_exists = session.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM information_schema.columns "
                    "  WHERE table_name = 'orders' AND column_name = 'expires_at'"
                    ")"
                )
            ).scalar()
            assert col_exp_exists is False
    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        admin_engine.dispose()
