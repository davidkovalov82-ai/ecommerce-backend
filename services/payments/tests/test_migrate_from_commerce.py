"""Проверка на повторяемия пренос на стари данни от Commerce БД към Payments БД на едноразова БД."""

import datetime
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings
from app.dependencies import get_stripe_client
from app.main import app
from app.models import (
    PaymentOutboxEvent,
    StripeCheckoutSession,
)
from app.stripe_client import StripeClient
from scripts.migrate_from_commerce import migrate_commerce_payment_state
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import (
    TEST_INTERNAL_TOKEN,
    TEST_WEBHOOK_SECRET,
    generate_stripe_signature,
)

client = TestClient(app)


def test_repeatable_cutover_migration_preserves_old_data_and_handles_legacy_webhooks(
    db_session: Session,
) -> None:
    """Симулира стара Commerce БД, пренася отворена сесия и обработен event ID, и проверява webhook след прехода."""
    source_db = "commerce_k5_legacy_source_test"
    admin_engine = create_engine(
        settings.admin_database_url, isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {source_db} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {source_db}"))

    source_url = (
        f"postgresql+psycopg://{settings.db_admin_user}:{settings.db_admin_password}"
        f"@{settings.db_host}:{settings.db_port}/{source_db}"
    )
    source_engine = create_engine(source_url)
    payments_engine = create_engine(TEST_DATABASE_URL)

    try:
        # 1. Създаваме минимална пред-К5 схема в едноразовата Commerce БД и я пълним със стари данни
        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=25)
        with source_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE orders (
                        id BIGINT PRIMARY KEY,
                        status VARCHAR(50) NOT NULL,
                        currency VARCHAR(3) NOT NULL,
                        total_minor BIGINT NOT NULL,
                        expires_at TIMESTAMPTZ NULL
                    );
                    CREATE TABLE stripe_checkout_sessions (
                        id BIGINT PRIMARY KEY,
                        order_id BIGINT NOT NULL,
                        status VARCHAR(50) NOT NULL,
                        amount_minor BIGINT NOT NULL,
                        currency VARCHAR(3) NOT NULL,
                        stripe_session_id VARCHAR(255) NULL,
                        checkout_url VARCHAR(1024) NULL,
                        idempotency_key VARCHAR(100) NOT NULL,
                        mode VARCHAR(10) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    CREATE TABLE payment_attempts (
                        id BIGINT PRIMARY KEY,
                        order_id BIGINT NOT NULL,
                        payment_method VARCHAR(50) NOT NULL,
                        status VARCHAR(50) NOT NULL,
                        amount_minor BIGINT NOT NULL,
                        currency VARCHAR(3) NOT NULL,
                        idempotency_key VARCHAR(36) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    CREATE TABLE stripe_processed_events (
                        id BIGINT PRIMARY KEY,
                        stripe_event_id VARCHAR(255) NOT NULL,
                        event_type VARCHAR(100) NOT NULL,
                        order_id BIGINT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    """
                )
            )
            conn.execute(
                text(
                    "INSERT INTO orders (id, status, currency, total_minor, expires_at) VALUES "
                    "(901, 'pending_payment', 'EUR', 6500, :exp), "
                    "(902, 'paid', 'EUR', 3000, NULL)"
                ),
                {"exp": future},
            )
            # Стара отворена сесия с id=15 (metadata['attempt_id'] = '15')
            conn.execute(
                text(
                    "INSERT INTO stripe_checkout_sessions "
                    "(id, order_id, status, amount_minor, currency, stripe_session_id, checkout_url, idempotency_key, mode) VALUES "
                    "(15, 901, 'open', 6500, 'EUR', 'cs_legacy_open_15', 'https://checkout.stripe.com/pay/cs_legacy_open_15', 'order_901_attempt_15', 'test'), "
                    "(16, 902, 'completed', 3000, 'EUR', 'cs_legacy_paid_16', 'https://checkout.stripe.com/pay/cs_legacy_paid_16', 'order_902_attempt_16', 'test')"
                )
            )
            # Стар неуспешен K2 опит за поръчка 901 + вече обработено Stripe събитие за поръчка 902
            conn.execute(
                text(
                    "INSERT INTO payment_attempts "
                    "(id, order_id, payment_method, status, amount_minor, currency, idempotency_key) VALUES "
                    "(3, 901, 'test_declined', 'declined', 6500, 'EUR', 'k2-legacy-declined-1')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO stripe_processed_events (id, stripe_event_id, event_type, order_id) VALUES "
                    "(7, 'evt_legacy_already_paid_902', 'checkout.session.completed', 902)"
                )
            )

        # 2. Първи пренос -> данните влизат в Payments
        stats1 = migrate_commerce_payment_state(source_engine, payments_engine)
        assert stats1 == {
            "sessions_inserted": 2,
            "legacy_attempts_inserted": 1,
            "processed_events_inserted": 1,
        }

        # Старите таблици в Commerce остават непокътнати
        with source_engine.connect() as c_conn:
            assert (
                c_conn.execute(
                    text("SELECT count(*) FROM stripe_checkout_sessions")
                ).scalar()
                == 2
            )

        # 3. Повторен пренос -> напълно идемпотентен (0 дубликати)
        stats2 = migrate_commerce_payment_state(source_engine, payments_engine)
        assert stats2 == {
            "sessions_inserted": 0,
            "legacy_attempts_inserted": 0,
            "processed_events_inserted": 0,
        }

        # 4. Стар webhook evt_legacy_already_paid_902 се повтаря след прехода -> игнориран, 0 нови outbox събития
        dup_legacy_payload = json.dumps(
            {
                "id": "evt_legacy_already_paid_902",
                "type": "checkout.session.completed",
                "livemode": False,
                "data": {
                    "object": {
                        "id": "cs_legacy_paid_16",
                        "livemode": False,
                        "payment_status": "paid",
                        "amount_total": 3000,
                        "currency": "eur",
                        "metadata": {"order_id": "902", "attempt_id": "16"},
                    }
                },
            }
        ).encode()
        with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
            r_dup = client.post(
                "/webhooks/stripe",
                content=dup_legacy_payload,
                headers={
                    "Stripe-Signature": generate_stripe_signature(
                        dup_legacy_payload, TEST_WEBHOOK_SECRET
                    )
                },
            )
            assert r_dup.status_code == 200
            assert r_dup.json()["status"] == "already_processed"
        assert db_session.query(PaymentOutboxEvent).filter_by(order_id=902).count() == 0

        # 5. Старата отворена сесия 15 (order 901) приема нов Stripe webhook след прехода!
        legacy_open_paid_payload = json.dumps(
            {
                "id": "evt_after_cutover_901",
                "type": "checkout.session.completed",
                "livemode": False,
                "data": {
                    "object": {
                        "id": "cs_legacy_open_15",
                        "livemode": False,
                        "payment_status": "paid",
                        "amount_total": 6500,
                        "currency": "eur",
                        "metadata": {"order_id": "901", "attempt_id": "15"},
                    }
                },
            }
        ).encode()
        with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
            r_paid = client.post(
                "/webhooks/stripe",
                content=legacy_open_paid_payload,
                headers={
                    "Stripe-Signature": generate_stripe_signature(
                        legacy_open_paid_payload, TEST_WEBHOOK_SECRET
                    )
                },
            )
            assert r_paid.status_code == 200
            assert r_paid.json()["status"] == "success"

        migrated_sess = db_session.get(StripeCheckoutSession, 15)
        assert migrated_sess is not None and migrated_sess.status == "completed"
        outbox_901 = db_session.query(PaymentOutboxEvent).filter_by(order_id=901).one()
        assert outbox_901.event_type == "payment.succeeded"
        assert outbox_901.payload["stripe_session_id"] == "cs_legacy_open_15"

        # 6. Синхронизираният sequence позволява създаване на нова сесия без конфликт на ID=15/16
        mock_stripe = MagicMock(spec=StripeClient)
        mock_stripe.create_checkout_session.return_value = SimpleNamespace(
            id="cs_post_cutover_903",
            url="https://checkout.stripe.com/pay/cs_post_cutover_903",
        )
        app.dependency_overrides[get_stripe_client] = lambda: mock_stripe
        try:
            r_new = client.post(
                "/internal/payments/checkout-sessions",
                json={
                    "order_id": 903,
                    "attempt_key": "order_903_attempt_1",
                    "amount_minor": 4200,
                    "currency": "EUR",
                    "line_items": [
                        {"name": "Item", "unit_amount": 4200, "quantity": 1}
                    ],
                },
                headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
            )
            assert r_new.status_code == 200
            new_sess = (
                db_session.query(StripeCheckoutSession).filter_by(order_id=903).one()
            )
            assert new_sess.id > 16
        finally:
            app.dependency_overrides.pop(get_stripe_client, None)
    finally:
        source_engine.dispose()
        payments_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {source_db} WITH (FORCE)"))
        admin_engine.dispose()
