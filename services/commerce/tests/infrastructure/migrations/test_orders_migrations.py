from pathlib import Path

import pytest
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


def test_shipments_and_shipping_address_migration_downgrade_policy() -> None:
    """Проверка на политиката за отказ на downgrade при наличие на shipment събития и успешно връщане при липсата им."""
    temp_db_name = "commerce_temp_shipment_mig_test"
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

        # 1. Миграция до head (e6f7a8b9c0d1)
        command.upgrade(alembic_cfg, "e6f7a8b9c0d1")

        # 2. Добавяне на потребители, поръчка, пратки и 2 shipment.shipped събития за една поръчка
        with Session(temp_engine) as session:
            session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) VALUES "
                    "(901, 'buyer901@example.com', 'h1', 'customer'), "
                    "(902, 'seller902@example.com', 'h2', 'customer'), "
                    "(903, 'seller903@example.com', 'h3', 'customer')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO orders (id, user_id, status, currency, total_minor, idempotency_key, fulfillment_status) "
                    "VALUES (9001, 901, 'paid', 'EUR', 10000, 'idemp-k3-mig', 'shipped')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO shipments (id, order_id, seller_id, status) VALUES "
                    "(5001, 9001, 902, 'shipped'), "
                    "(5002, 9001, 903, 'shipped')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO order_outbox_events (order_id, shipment_id, event_type, payload, status) VALUES "
                    "(9001, 5001, 'shipment.shipped', '{\"shipment_id\": 5001}', 'sent'), "
                    "(9001, 5002, 'shipment.shipped', '{\"shipment_id\": 5002}', 'sent')"
                )
            )
            session.commit()

        # 3. Опит за downgrade към d5e6f7a8b9c0 при налични shipment събития -> очаква се RuntimeError
        with pytest.raises(
            Exception,
            match="Cannot downgrade migration e6f7a8b9c0d1: found 2 shipment events",
        ):
            command.downgrade(alembic_cfg, "d5e6f7a8b9c0")

        # 4. Проверка на целостта на данните след отхвърления отказ (транзакционен DDL)
        with Session(temp_engine) as session:
            # Таблицата shipments продължава да съществува с двете си пратки
            sh_count = session.execute(
                text("SELECT count(*) FROM shipments WHERE order_id = 9001")
            ).scalar()
            assert sh_count == 2

            # Събитията в order_outbox_events са непокътнати
            ev_count = session.execute(
                text(
                    "SELECT count(*) FROM order_outbox_events WHERE order_id = 9001 AND event_type = 'shipment.shipped'"
                )
            ).scalar()
            assert ev_count == 2

            # Поръчката пази fulfillment_status
            f_status = session.execute(
                text("SELECT fulfillment_status FROM orders WHERE id = 9001")
            ).scalar()
            assert f_status == "shipped"

            # 5. Изчистване на shipment събитията (но запазваме поръчката и обичайни outbox събития)
            session.execute(
                text("DELETE FROM order_outbox_events WHERE shipment_id IS NOT NULL")
            )
            session.execute(
                text(
                    "INSERT INTO order_outbox_events (order_id, event_type, payload, status) "
                    "VALUES (9001, 'order.paid', '{\"order_id\": 9001}', 'sent')"
                )
            )
            session.commit()

        # 6. Downgrade без shipment събития трябва да мине успешно
        command.downgrade(alembic_cfg, "d5e6f7a8b9c0")

        with Session(temp_engine) as session:
            # Таблицата shipments е премахната
            sh_exists = session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'shipments')"
                )
            ).scalar()
            assert sh_exists is False

            # Колоната shipment_id в order_outbox_events е премахната
            col_exists = session.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM information_schema.columns "
                    "  WHERE table_name = 'order_outbox_events' AND column_name = 'shipment_id'"
                    ")"
                )
            ).scalar()
            assert col_exists is False

            # Ограничението (order_id, event_type) е възстановено
            ev_rem = session.execute(
                text("SELECT event_type FROM order_outbox_events WHERE order_id = 9001")
            ).scalar()
            assert ev_rem == "order.paid"

            # Поръчката съществува
            ord_exists = session.execute(
                text("SELECT id FROM orders WHERE id = 9001")
            ).scalar()
            assert ord_exists == 9001

        # 7. Обратен upgrade до head също работи чисто
        command.upgrade(alembic_cfg, "head")

        with Session(temp_engine) as session:
            sh_recreated = session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'shipments')"
                )
            ).scalar()
            assert sh_recreated is True
    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        admin_engine.dispose()


def test_k4_outbox_migration_preserves_legacy_sent_and_pending_events() -> None:
    """К4 миграция f7a8b9c0d1e2: историческите 'sent' -> 'legacy_sent' без повторно пращане, 'pending' се запазват."""
    from unittest.mock import MagicMock

    from sqlalchemy.orm import sessionmaker

    from app.modules.orders.outbox_processor import OutboxProcessor
    from app.modules.orders.publisher import EventPublisher

    temp_db_name = "commerce_temp_k4_outbox_mig_test"
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

        # 1. Старт в състояние преди К4 (e6f7a8b9c0d1)
        command.upgrade(alembic_cfg, "e6f7a8b9c0d1")

        with Session(temp_engine) as session:
            session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) VALUES "
                    "(951, 'legacy_buyer@example.com', 'h1', 'customer'), "
                    "(952, 'legacy_seller@example.com', 'h2', 'customer')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO orders (id, user_id, status, currency, total_minor, idempotency_key) VALUES "
                    "(9501, 951, 'paid', 'EUR', 2500, 'idemp-k4-1'), "
                    "(9502, 951, 'paid', 'EUR', 4000, 'idemp-k4-2')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO shipments (id, order_id, seller_id, status) VALUES "
                    "(6001, 9501, 952, 'shipped'), "
                    "(6002, 9502, 952, 'shipped')"
                )
            )
            # Исторически вече изпратени чрез стария SMTP outbox ('sent') и неизпратени ('pending') без телефон
            session.execute(
                text(
                    "INSERT INTO order_outbox_events "
                    "(id, order_id, shipment_id, event_type, payload, status, processed_at) VALUES "
                    "(101, 9501, NULL, 'order.paid', "
                    '\'{"order_id": 9501, "customer_email": "legacy_buyer@example.com", "total_minor": 2500, "currency": "EUR"}\', '
                    "'sent', '2026-11-08 12:00:00+00'), "
                    "(102, 9501, 6001, 'shipment.shipped', "
                    '\'{"order_id": 9501, "shipment_id": 6001, "customer_email": "legacy_buyer@example.com", "items": []}\', '
                    "'sent', '2026-11-08 13:00:00+00'), "
                    "(103, 9502, NULL, 'order.paid', "
                    '\'{"order_id": 9502, "customer_email": "legacy_buyer@example.com", "total_minor": 4000, "currency": "EUR"}\', '
                    "'pending', NULL), "
                    "(104, 9502, 6002, 'shipment.shipped', "
                    '\'{"order_id": 9502, "shipment_id": 6002, "customer_email": "legacy_buyer@example.com", "items": []}\', '
                    "'pending', NULL)"
                )
            )
            session.commit()

        # 2. Обновяване до head (f7a8b9c0d1e2)
        command.upgrade(alembic_cfg, "head")

        with Session(temp_engine) as session:
            rows = session.execute(
                text(
                    "SELECT id, status, event_id, event_version, processed_at, published_at, occurred_at "
                    "FROM order_outbox_events ORDER BY id"
                )
            ).all()
            assert len(rows) == 4
            by_id = {r.id: r for r in rows}

            # Исторически изпратените стават 'legacy_sent' и пазят processed_at
            assert by_id[101].status == "legacy_sent"
            assert by_id[101].processed_at is not None
            assert by_id[101].published_at is None
            assert by_id[102].status == "legacy_sent"
            assert by_id[102].processed_at is not None

            # Чакащите остават 'pending'
            assert by_id[103].status == "pending"
            assert by_id[103].processed_at is None
            assert by_id[104].status == "pending"

            # Всички имат стабилен уникален event_id, version=1 и occurred_at
            event_ids = {r.event_id for r in rows}
            assert len(event_ids) == 4
            for r in rows:
                assert r.event_version == 1
                assert r.occurred_at is not None

        # 3. Публикуване чрез OutboxProcessor: изпраща САМО 'pending' (103, 104), НЕ 'legacy_sent' (101, 102)
        TempSessionLocal = sessionmaker(
            bind=temp_engine, autoflush=False, expire_on_commit=False
        )
        mock_pub = MagicMock(spec=EventPublisher)
        processor = OutboxProcessor(
            session_factory=TempSessionLocal, publisher=mock_pub
        )
        stats = processor.process_pending_events(limit=50)
        assert stats == {"processed": 2, "published": 2, "failed": 0}
        published_order_ids = [
            call.kwargs["envelope"]["data"]["order_id"]
            for call in mock_pub.publish_event.call_args_list
        ]
        assert published_order_ids == [9502, 9502]

        # 4. Безопасен downgrade до e6f7a8b9c0d1 връща статуса на 'sent' без загуба
        command.downgrade(alembic_cfg, "e6f7a8b9c0d1")
        with Session(temp_engine) as session:
            statuses = dict(
                session.execute(
                    text("SELECT id, status FROM order_outbox_events ORDER BY id")
                ).all()
            )
            assert statuses == {
                101: "sent",
                102: "sent",
                103: "sent",
                104: "sent",
            }
    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        admin_engine.dispose()
