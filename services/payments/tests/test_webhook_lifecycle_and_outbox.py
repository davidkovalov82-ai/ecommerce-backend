"""Тестове на Stripe webhooks, конфигурация, Outbox и идемпотентност в независимия Payments сервис."""

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.dependencies import get_stripe_client
from app.main import app
from app.models import (
    PaymentAttempt,
    PaymentDiscrepancy,
    PaymentOutboxEvent,
    StripeCheckoutSession,
    StripeProcessedEvent,
)
from app.outbox_processor import OutboxProcessor
from app.publisher import BrokerUnavailableError, EventPublisher
from app.stripe_client import StripeClient
from tests.helpers import (
    TEST_INTERNAL_TOKEN,
    TEST_WEBHOOK_SECRET,
    generate_stripe_signature,
)

client = TestClient(app)


def _seed_attempt_and_session(
    db_session: Session,
    *,
    order_id: int,
    attempt_key: str,
    status: str = "open",
    session_status: str = "open",
    stripe_session_id: str | None = "cs_seed_1",
    amount_minor: int = 5000,
    mode: str = "test",
) -> tuple[PaymentAttempt, StripeCheckoutSession]:
    attempt = PaymentAttempt(
        order_id=order_id,
        attempt_key=attempt_key,
        status=status,
        amount_minor=amount_minor,
        currency="EUR",
        mode=mode,
    )
    db_session.add(attempt)
    db_session.flush()
    sess = StripeCheckoutSession(
        payment_attempt_id=attempt.id,
        order_id=order_id,
        status=session_status,
        amount_minor=amount_minor,
        currency="EUR",
        mode=mode,
        stripe_session_id=stripe_session_id,
        checkout_url=(
            f"https://checkout.stripe.com/pay/{stripe_session_id}"
            if stripe_session_id
            else None
        ),
        idempotency_key=attempt_key,
    )
    db_session.add(sess)
    db_session.commit()
    return attempt, sess


def test_stripe_config_validation_and_secret_not_leaked(db_session: Session) -> None:
    """Валидация на STRIPE_MODE и забрана секретите да изтичат в HTTP грешките."""
    with pytest.raises(
        ValidationError, match="STRIPE_SECRET_KEY must start with 'sk_live_'"
    ):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_test_12345",
            stripe_webhook_secret="whsec_12345",
            stripe_success_url="https://example.com/s/{order_id}",
            stripe_cancel_url="https://example.com/c/{order_id}",
        )

    with pytest.raises(
        ValidationError, match="STRIPE_API_BASE override is not allowed"
    ):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_live_12345",
            stripe_webhook_secret="whsec_12345",
            stripe_api_base="http://localhost:9999",
            stripe_success_url="https://example.com/s/{order_id}",
            stripe_cancel_url="https://example.com/c/{order_id}",
        )

    secret_val = "sk_test_super_secret_leak_check"
    mock_client = MagicMock(spec=StripeClient)
    mock_client.create_checkout_session.side_effect = RuntimeError(
        f"boom with {secret_val}"
    )
    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        with patch.object(settings, "stripe_secret_key", secret_val):
            resp = client.post(
                "/internal/payments/checkout-sessions",
                json={
                    "order_id": 501,
                    "attempt_key": "order_501_attempt_1",
                    "amount_minor": 1500,
                    "currency": "EUR",
                    "line_items": [{"name": "P", "unit_amount": 1500, "quantity": 1}],
                },
                headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
            )
            assert resp.status_code == 502
            assert secret_val not in resp.text
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_webhook_signature_mode_and_mismatch_guards(db_session: Session) -> None:
    """Проверка на суровия подпис (без JWT), липса на секрет, livemode, attempt_id, сума и валута."""
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        r_no_sig = client.post("/webhooks/stripe", content=b"{}")
        assert r_no_sig.status_code == 400
        assert r_no_sig.json()["detail"]["code"] == "INVALID_STRIPE_SIGNATURE"

        r_bad_sig = client.post(
            "/webhooks/stripe",
            content=b'{"id": "evt_1"}',
            headers={"Stripe-Signature": "t=1,v1=deadbeef"},
        )
        assert r_bad_sig.status_code == 400

    with patch.object(settings, "stripe_webhook_secret", None):
        r_unconf = client.post(
            "/webhooks/stripe",
            content=b'{"id": "evt_1"}',
            headers={"Stripe-Signature": "t=1,v1=deadbeef"},
        )
        assert r_unconf.status_code == 503

    _, sess = _seed_attempt_and_session(
        db_session,
        order_id=300,
        attempt_key="order_300_a1",
        stripe_session_id="cs_guard_300",
        amount_minor=4000,
    )

    for label, evt_obj in (
        (
            "mode_mismatch",
            {
                "id": "evt_mode_1",
                "type": "checkout.session.completed",
                "livemode": True,
                "data": {"object": {"id": "cs_guard_300", "livemode": True}},
            },
        ),
        (
            "invalid_attempt",
            {
                "id": "evt_att_1",
                "type": "checkout.session.completed",
                "livemode": False,
                "data": {
                    "object": {
                        "id": "cs_other_999",
                        "livemode": False,
                        "payment_status": "paid",
                        "amount_total": 4000,
                        "currency": "eur",
                        "metadata": {"order_id": "300", "attempt_id": "999999"},
                    }
                },
            },
        ),
        (
            "amount_mismatch",
            {
                "id": "evt_amt_1",
                "type": "checkout.session.completed",
                "livemode": False,
                "data": {
                    "object": {
                        "id": "cs_guard_300",
                        "livemode": False,
                        "payment_status": "paid",
                        "amount_total": 100,
                        "currency": "eur",
                        "metadata": {"order_id": "300", "attempt_id": str(sess.id)},
                    }
                },
            },
        ),
        (
            "currency_mismatch",
            {
                "id": "evt_curr_1",
                "type": "checkout.session.completed",
                "livemode": False,
                "data": {
                    "object": {
                        "id": "cs_guard_300",
                        "livemode": False,
                        "payment_status": "paid",
                        "amount_total": 4000,
                        "currency": "usd",
                        "metadata": {"order_id": "300", "attempt_id": str(sess.id)},
                    }
                },
            },
        ),
    ):
        raw = json.dumps(evt_obj).encode()
        sig = generate_stripe_signature(raw, TEST_WEBHOOK_SECRET)
        with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
            resp = client.post(
                "/webhooks/stripe",
                content=raw,
                headers={"Stripe-Signature": sig},
            )
            assert resp.status_code == 200, label
            assert resp.json()["reason"] == label

    assert db_session.query(PaymentOutboxEvent).count() == 0


def test_webhook_standalone_saves_payment_succeeded_and_is_concurrent_idempotent(
    db_session: Session,
) -> None:
    """Webhook приема плащането напълно без Commerce и записва само 1 payment.succeeded в Outbox."""
    attempt, sess = _seed_attempt_and_session(
        db_session,
        order_id=310,
        attempt_key="order_310_a1",
        status="initiated",
        session_status="initiated",
        stripe_session_id=None,
        amount_minor=6000,
    )

    event_payload = {
        "id": "evt_paid_standalone_310",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_standalone_310",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 6000,
                "currency": "eur",
                "client_reference_id": "310",
                "metadata": {"order_id": "310", "attempt_id": str(sess.id)},
            }
        },
    }
    raw = json.dumps(event_payload).encode()
    sig = generate_stripe_signature(raw, TEST_WEBHOOK_SECRET)

    def _post() -> tuple[int, dict[str, Any]]:
        tc = TestClient(app)
        with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
            r = tc.post(
                "/webhooks/stripe",
                content=raw,
                headers={"Stripe-Signature": sig},
            )
            return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_post)
        f2 = pool.submit(_post)
        c1, d1 = f1.result()
        c2, d2 = f2.result()

    assert c1 == 200 and c2 == 200
    assert "success" in (d1["status"], d2["status"])

    db_session.refresh(attempt)
    db_session.refresh(sess)
    assert attempt.status == "succeeded"
    assert sess.status == "completed"
    assert sess.stripe_session_id == "cs_standalone_310"

    assert (
        db_session.query(StripeProcessedEvent)
        .filter_by(stripe_event_id="evt_paid_standalone_310")
        .count()
        == 1
    )
    outbox = db_session.query(PaymentOutboxEvent).filter_by(order_id=310).all()
    assert len(outbox) == 1
    assert outbox[0].event_type == "payment.succeeded"
    assert outbox[0].payment_attempt_id == attempt.id
    assert outbox[0].payload["stripe_session_id"] == "cs_standalone_310"
    assert outbox[0].payload["attempt_key"] == "order_310_a1"


def test_webhook_delayed_payment_out_of_order_and_failure_lifecycle(
    db_session: Session,
) -> None:
    """completed(unpaid) -> processing; out-of-order unpaid след succeeded не сваля статуса; failed/expired пишат outbox."""
    attempt, sess = _seed_attempt_and_session(
        db_session,
        order_id=320,
        attempt_key="order_320_a1",
        stripe_session_id="cs_async_320",
        amount_minor=7500,
    )

    # 1. Пристига async_payment_succeeded първо
    raw_succ = json.dumps(
        {
            "id": "evt_async_succ_320",
            "type": "checkout.session.async_payment_succeeded",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_async_320",
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": 7500,
                    "currency": "eur",
                    "metadata": {"order_id": "320", "attempt_id": str(sess.id)},
                }
            },
        }
    ).encode()
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        r1 = client.post(
            "/webhooks/stripe",
            content=raw_succ,
            headers={
                "Stripe-Signature": generate_stripe_signature(
                    raw_succ, TEST_WEBHOOK_SECRET
                )
            },
        )
        assert r1.status_code == 200

    # 2. Закъснял completed(unpaid) не променя succeeded към processing
    raw_unpaid = json.dumps(
        {
            "id": "evt_late_unpaid_320",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_async_320",
                    "livemode": False,
                    "payment_status": "unpaid",
                    "amount_total": 7500,
                    "currency": "eur",
                    "metadata": {"order_id": "320", "attempt_id": str(sess.id)},
                }
            },
        }
    ).encode()
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        r2 = client.post(
            "/webhooks/stripe",
            content=raw_unpaid,
            headers={
                "Stripe-Signature": generate_stripe_signature(
                    raw_unpaid, TEST_WEBHOOK_SECRET
                )
            },
        )
        assert r2.status_code == 200

    db_session.refresh(attempt)
    assert attempt.status == "succeeded"
    outbox_types = [
        e.event_type
        for e in db_session.query(PaymentOutboxEvent).filter_by(order_id=320).all()
    ]
    assert outbox_types == ["payment.succeeded"]

    # 3. Нов ред за async_payment_failed -> payment.failed в Outbox
    attempt_fail, sess_fail = _seed_attempt_and_session(
        db_session,
        order_id=321,
        attempt_key="order_321_a1",
        stripe_session_id="cs_async_fail_321",
        amount_minor=4500,
    )
    raw_fail = json.dumps(
        {
            "id": "evt_async_fail_321",
            "type": "checkout.session.async_payment_failed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_async_fail_321",
                    "livemode": False,
                    "metadata": {
                        "order_id": "321",
                        "attempt_id": str(sess_fail.id),
                    },
                }
            },
        }
    ).encode()
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        rf = client.post(
            "/webhooks/stripe",
            content=raw_fail,
            headers={
                "Stripe-Signature": generate_stripe_signature(
                    raw_fail, TEST_WEBHOOK_SECRET
                )
            },
        )
        assert rf.status_code == 200
        assert rf.json()["status"] == "failed"

    db_session.refresh(attempt_fail)
    assert attempt_fail.status == "failed"
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=321, event_type="payment.failed")
        .count()
        == 1
    )


def _post_signed_webhook(event_payload: dict[str, Any]):
    body = json.dumps(event_payload).encode("utf-8")
    sig = generate_stripe_signature(body, TEST_WEBHOOK_SECRET)
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        return client.post(
            "/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
        )


def test_webhook_paid_and_mode_mismatches_reject_and_record_discrepancy(
    db_session: Session,
) -> None:
    """Webhook проверките за сума/валута/livemode/сесия отказват paid преход и записват PaymentDiscrepancy."""
    attempt, _ = _seed_attempt_and_session(
        db_session,
        order_id=325,
        attempt_key="order_325_attempt_1",
        status="processing",
        session_status="processing",
        stripe_session_id="cs_guard_325",
        amount_minor=5000,
        mode="test",
    )

    # 1. livemode=True при STRIPE_MODE=test
    r_mode = _post_signed_webhook(
        {
            "id": "evt_guard_mode_1",
            "type": "checkout.session.completed",
            "livemode": True,
            "data": {
                "object": {
                    "id": "cs_guard_325",
                    "livemode": True,
                    "payment_status": "paid",
                    "amount_total": 5000,
                    "currency": "eur",
                    "client_reference_id": "325",
                    "metadata": {
                        "order_id": "325",
                        "attempt_key": "order_325_attempt_1",
                        "payment_attempt_id": str(attempt.id),
                    },
                }
            },
        }
    )
    assert r_mode.status_code == 200
    assert r_mode.json()["status"] == "ignored"
    assert r_mode.json()["reason"] == "mode_mismatch"

    # 2. Несъответстващ session.id спрямо записаната сесия
    r_sess = _post_signed_webhook(
        {
            "id": "evt_guard_sess_1",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_foreign_999",
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": 5000,
                    "currency": "eur",
                    "client_reference_id": "325",
                    "metadata": {
                        "order_id": "325",
                        "attempt_key": "order_325_attempt_1",
                        "payment_attempt_id": str(attempt.id),
                    },
                }
            },
        }
    )
    assert r_sess.status_code == 200
    assert r_sess.json()["status"] == "mismatched"
    assert r_sess.json()["reason"] == "session_id_mismatch"

    # 3. Разминаване в async_payment_succeeded: сума, валута, чужд order_id в metadata
    mismatch_cases = [
        (
            "evt_guard_amt_1",
            {
                "id": "cs_guard_325",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 4900,
                "currency": "eur",
                "client_reference_id": "325",
                "metadata": {
                    "order_id": "325",
                    "attempt_key": "order_325_attempt_1",
                    "payment_attempt_id": str(attempt.id),
                },
            },
            "amount_mismatch",
        ),
        (
            "evt_guard_curr_1",
            {
                "id": "cs_guard_325",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "usd",
                "client_reference_id": "325",
                "metadata": {
                    "order_id": "325",
                    "attempt_key": "order_325_attempt_1",
                    "payment_attempt_id": str(attempt.id),
                },
            },
            "currency_mismatch",
        ),
        (
            "evt_guard_meta_order_1",
            {
                "id": "cs_guard_325",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "eur",
                "client_reference_id": "325",
                "metadata": {
                    "order_id": "99999",
                    "attempt_key": "order_325_attempt_1",
                    "payment_attempt_id": str(attempt.id),
                },
            },
            "invalid_attempt",
        ),
        (
            "evt_guard_cref_order_1",
            {
                "id": "cs_guard_325",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "eur",
                "client_reference_id": "99999",
                "metadata": {
                    "order_id": "325",
                    "attempt_key": "order_325_attempt_1",
                    "payment_attempt_id": str(attempt.id),
                },
            },
            "session_order_mismatch",
        ),
    ]
    for event_id, session_obj, expected_reason in mismatch_cases:
        resp = _post_signed_webhook(
            {
                "id": event_id,
                "type": "checkout.session.async_payment_succeeded",
                "livemode": False,
                "data": {"object": session_obj},
            }
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "mismatched"
        assert resp.json()["reason"] == expected_reason

    db_session.expire_all()
    db_session.refresh(attempt)
    assert attempt.status == "processing"
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=325, event_type="payment.succeeded")
        .count()
        == 0
    )
    discrepancies = (
        db_session.query(PaymentDiscrepancy)
        .filter_by(source="webhook")
        .order_by(PaymentDiscrepancy.id.asc())
        .all()
    )
    reasons = {row.reason for row in discrepancies}
    assert {
        "mode_mismatch",
        "session_id_mismatch",
        "amount_mismatch",
        "currency_mismatch",
        "invalid_attempt",
        "session_order_mismatch",
    }.issubset(reasons)


def test_out_of_order_async_failed_then_late_completed_unpaid_preserves_retry(
    db_session: Session,
) -> None:
    """Закъсняло checkout.session.completed(unpaid) след async_payment_failed не връща failed опита в processing."""
    attempt_1, _ = _seed_attempt_and_session(
        db_session,
        order_id=326,
        attempt_key="order_326_attempt_1",
        status="open",
        session_status="open",
        stripe_session_id="cs_ooo_fail_1",
        amount_minor=5000,
        mode="test",
    )

    r_fail = _post_signed_webhook(
        {
            "id": "evt_ooo_fail_1",
            "type": "checkout.session.async_payment_failed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_ooo_fail_1",
                    "livemode": False,
                    "payment_status": "unpaid",
                    "client_reference_id": "326",
                    "metadata": {
                        "order_id": "326",
                        "attempt_key": "order_326_attempt_1",
                        "payment_attempt_id": str(attempt_1.id),
                    },
                }
            },
        }
    )
    assert r_fail.status_code == 200
    assert r_fail.json()["status"] == "failed"

    attempt_2, _ = _seed_attempt_and_session(
        db_session,
        order_id=326,
        attempt_key="order_326_attempt_2",
        status="open",
        session_status="open",
        stripe_session_id="cs_ooo_retry_2",
        amount_minor=5000,
        mode="test",
    )

    # Закъсняло completed(unpaid) за стария attempt_1 не може да го върне към processing
    r_late_unpaid = _post_signed_webhook(
        {
            "id": "evt_ooo_late_unpaid_1",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_ooo_fail_1",
                    "livemode": False,
                    "payment_status": "unpaid",
                    "amount_total": 5000,
                    "currency": "eur",
                    "client_reference_id": "326",
                    "metadata": {
                        "order_id": "326",
                        "attempt_key": "order_326_attempt_1",
                        "payment_attempt_id": str(attempt_1.id),
                    },
                }
            },
        }
    )
    assert r_late_unpaid.status_code == 200
    assert r_late_unpaid.json()["status"] == "ignored"

    db_session.expire_all()
    db_session.refresh(attempt_1)
    db_session.refresh(attempt_2)
    assert attempt_1.status == "failed"
    assert attempt_2.status == "open"
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=326, event_type="payment.processing")
        .count()
        == 0
    )

    # Повторният опит attempt_2 остава използваем и завършва успешно
    r_retry_paid = _post_signed_webhook(
        {
            "id": "evt_ooo_retry_paid_2",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": "cs_ooo_retry_2",
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": 5000,
                    "currency": "eur",
                    "client_reference_id": "326",
                    "metadata": {
                        "order_id": "326",
                        "attempt_key": "order_326_attempt_2",
                        "payment_attempt_id": str(attempt_2.id),
                    },
                }
            },
        }
    )
    assert r_retry_paid.status_code == 200
    assert r_retry_paid.json()["status"] == "success"

    db_session.expire_all()
    db_session.refresh(attempt_1)
    db_session.refresh(attempt_2)
    assert attempt_1.status == "failed"
    assert attempt_2.status == "succeeded"
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=326, event_type="payment.succeeded")
        .count()
        == 1
    )


def test_outbox_processor_keeps_pending_when_rabbitmq_down_and_publishes_after_recovery(
    db_session: Session,
    session_factory,
) -> None:
    """Когато RabbitMQ е недостъпен, Outbox пази pending със същия event_id и публикува при възстановяване."""
    attempt, _ = _seed_attempt_and_session(
        db_session,
        order_id=330,
        attempt_key="order_330_a1",
        status="succeeded",
        session_status="completed",
        stripe_session_id="cs_outbox_330",
    )
    ev = PaymentOutboxEvent(
        event_id="11111111-1111-4111-8111-111111111330",
        event_version=1,
        payment_attempt_id=attempt.id,
        order_id=330,
        event_type="payment.succeeded",
        payload={
            "order_id": 330,
            "payment_attempt_id": attempt.id,
            "attempt_key": "order_330_a1",
            "stripe_session_id": "cs_outbox_330",
            "stripe_event_id": "evt_330",
            "amount_minor": 5000,
            "currency": "EUR",
            "mode": "test",
        },
        status="pending",
    )
    db_session.add(ev)
    db_session.commit()

    failing_pub = MagicMock(spec=EventPublisher)
    failing_pub.publish_event.side_effect = BrokerUnavailableError("rabbitmq down")
    proc_down = OutboxProcessor(session_factory=session_factory, publisher=failing_pub)
    stats_down = proc_down.process_pending_events(limit=10)
    assert stats_down == {"processed": 1, "published": 0, "failed": 1}

    db_session.refresh(ev)
    assert ev.status == "pending"
    assert ev.retry_count == 1
    assert ev.event_id == "11111111-1111-4111-8111-111111111330"

    ok_pub = MagicMock(spec=EventPublisher)
    proc_ok = OutboxProcessor(session_factory=session_factory, publisher=ok_pub)
    stats_ok = proc_ok.process_pending_events(limit=10)
    assert stats_ok == {"processed": 1, "published": 1, "failed": 0}
    ok_pub.publish_event.assert_called_once()

    db_session.refresh(ev)
    assert ev.status == "published"
    assert (
        db_session.execute(
            text("SELECT count(*) FROM payment_outbox_events WHERE status = 'pending'")
        ).scalar()
        == 0
    )
