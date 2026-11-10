"""Тестове за /internal/payments/orders/{id}/cancel и сверка със Stripe (reconciliation)."""

import datetime
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.dependencies import get_stripe_client
from app.main import app
from app.models import (
    PaymentAttempt,
    PaymentDiscrepancy,
    PaymentOutboxEvent,
    StripeCheckoutSession,
)
from app.repository import PaymentRepository
from app.service import PaymentService
from app.stripe_client import StripeClient
from tests.helpers import (
    TEST_INTERNAL_TOKEN,
    TEST_WEBHOOK_SECRET,
    generate_stripe_signature,
)

client = TestClient(app)


def test_cancel_guards_for_initiated_processing_timeout_and_already_paid_race(
    db_session: Session,
) -> None:
    """Отказ: initiated/processing -> 409, Stripe timeout -> 502 без expire, complete/paid конфликт -> 409 ALREADY_PAID."""
    # 1. initiated без Stripe ID -> 409
    a_init = PaymentAttempt(
        order_id=401,
        attempt_key="order_401_a1",
        status="initiated",
        amount_minor=2000,
        currency="EUR",
    )
    db_session.add(a_init)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a_init.id,
            order_id=401,
            status="initiated",
            amount_minor=2000,
            currency="EUR",
            idempotency_key="order_401_a1",
        )
    )

    # 2. processing -> 409
    a_proc = PaymentAttempt(
        order_id=402,
        attempt_key="order_402_a1",
        status="processing",
        amount_minor=2000,
        currency="EUR",
    )
    db_session.add(a_proc)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a_proc.id,
            order_id=402,
            status="processing",
            amount_minor=2000,
            currency="EUR",
            stripe_session_id="cs_proc_402",
            idempotency_key="order_402_a1",
        )
    )

    # 3. open сесия при timeout на expire -> връща 502 и НЕ я маркира expired
    a_open = PaymentAttempt(
        order_id=403,
        attempt_key="order_403_a1",
        status="open",
        amount_minor=2000,
        currency="EUR",
    )
    db_session.add(a_open)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a_open.id,
            order_id=403,
            status="open",
            amount_minor=2000,
            currency="EUR",
            stripe_session_id="cs_timeout_403",
            checkout_url="https://checkout.stripe.com/pay/cs_timeout_403",
            idempotency_key="order_403_a1",
        )
    )

    # 4. open сесия, която Stripe вече е приел като complete/paid в същия момент -> плащането побеждава!
    a_race = PaymentAttempt(
        order_id=404,
        attempt_key="order_404_a1",
        status="open",
        amount_minor=3500,
        currency="EUR",
    )
    db_session.add(a_race)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a_race.id,
            order_id=404,
            status="open",
            amount_minor=3500,
            currency="EUR",
            stripe_session_id="cs_race_paid_404",
            checkout_url="https://checkout.stripe.com/pay/cs_race_paid_404",
            idempotency_key="order_404_a1",
        )
    )
    db_session.commit()

    assert (
        client.post(
            "/internal/payments/orders/401/cancel",
            json={"reason": "cancel"},
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/internal/payments/orders/402/cancel",
            json={"reason": "cancel"},
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        ).status_code
        == 409
    )

    mock_client = MagicMock(spec=StripeClient)

    def _expire_side_effect(sid: str) -> Any:
        if sid == "cs_timeout_403":
            raise TimeoutError("Stripe timeout on expire")
        if sid == "cs_race_paid_404":
            raise RuntimeError(
                "Cannot expire a Checkout Session whose status is complete"
            )
        raise AssertionError(sid)

    mock_client.expire_checkout_session.side_effect = _expire_side_effect
    mock_client.retrieve_checkout_session.return_value = SimpleNamespace(
        id="cs_race_paid_404",
        livemode=False,
        status="complete",
        payment_status="paid",
        amount_total=3500,
        currency="eur",
        client_reference_id="404",
        metadata={"order_id": "404", "attempt_key": "order_404_a1"},
    )
    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        r_timeout = client.post(
            "/internal/payments/orders/403/cancel",
            json={"reason": "cancel"},
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r_timeout.status_code == 502
        db_session.refresh(a_open)
        assert a_open.status == "open"

        r_race = client.post(
            "/internal/payments/orders/404/cancel",
            json={"reason": "cancel"},
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r_race.status_code == 409
        assert r_race.json()["detail"]["code"] == "ORDER_ALREADY_PAID"
        db_session.refresh(a_race)
        assert a_race.status == "succeeded"
        assert (
            db_session.query(PaymentOutboxEvent)
            .filter_by(order_id=404, event_type="payment.succeeded")
            .count()
            == 1
        )
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_reconcile_unresolved_payments_branches(db_session: Session) -> None:
    """Сверка: timeout Stripe пази резерва; paid -> payment.succeeded; open+TTL -> expire + payment.expired."""
    now = datetime.datetime.now(datetime.UTC)
    past = now - datetime.timedelta(minutes=30)

    # 1. open + TTL expired -> Stripe open -> прекратява в Stripe -> payment.expired
    a1 = PaymentAttempt(
        order_id=451,
        attempt_key="order_451_a1",
        status="open",
        amount_minor=2000,
        currency="EUR",
        order_expires_at=past,
    )
    db_session.add(a1)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a1.id,
            order_id=451,
            status="open",
            amount_minor=2000,
            currency="EUR",
            stripe_session_id="cs_rec_exp_451",
            idempotency_key="order_451_a1",
        )
    )

    # 2. open + Stripe down -> deferred
    a2 = PaymentAttempt(
        order_id=452,
        attempt_key="order_452_a1",
        status="open",
        amount_minor=2000,
        currency="EUR",
        order_expires_at=past,
    )
    db_session.add(a2)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a2.id,
            order_id=452,
            status="open",
            amount_minor=2000,
            currency="EUR",
            stripe_session_id="cs_rec_down_452",
            idempotency_key="order_452_a1",
        )
    )

    # 3. open + Stripe paid -> succeeded + payment.succeeded
    a3 = PaymentAttempt(
        order_id=453,
        attempt_key="order_453_a1",
        status="open",
        amount_minor=2000,
        currency="EUR",
        order_expires_at=past,
    )
    db_session.add(a3)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a3.id,
            order_id=453,
            status="open",
            amount_minor=2000,
            currency="EUR",
            stripe_session_id="cs_rec_paid_453",
            idempotency_key="order_453_a1",
        )
    )

    # 4. processing + TTL expired -> deferred (не освобождава резерва при отложено плащане!)
    a4 = PaymentAttempt(
        order_id=454,
        attempt_key="order_454_a1",
        status="processing",
        amount_minor=2000,
        currency="EUR",
        order_expires_at=past,
    )
    db_session.add(a4)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a4.id,
            order_id=454,
            status="processing",
            amount_minor=2000,
            currency="EUR",
            stripe_session_id="cs_rec_proc_454",
            idempotency_key="order_454_a1",
        )
    )
    db_session.commit()

    mock_stripe = MagicMock(spec=StripeClient)

    def _retrieve(sid: str) -> Any:
        if sid == "cs_rec_exp_451":
            return SimpleNamespace(
                id="cs_rec_exp_451",
                livemode=False,
                status="open",
                payment_status="unpaid",
            )
        if sid == "cs_rec_down_452":
            raise TimeoutError("Stripe timeout")
        if sid == "cs_rec_paid_453":
            return SimpleNamespace(
                id="cs_rec_paid_453",
                livemode=False,
                status="complete",
                payment_status="paid",
                amount_total=2000,
                currency="eur",
                client_reference_id="453",
                metadata={"order_id": "453", "attempt_key": "order_453_a1"},
            )
        if sid == "cs_rec_proc_454":
            return SimpleNamespace(
                id="cs_rec_proc_454",
                livemode=False,
                status="complete",
                payment_status="unpaid",
            )
        raise AssertionError(sid)

    mock_stripe.retrieve_checkout_session.side_effect = _retrieve

    service = PaymentService(
        repository=PaymentRepository(db_session),
        session=db_session,
        stripe_client=mock_stripe,
    )
    stats = service.reconcile_unresolved_payments(now=now)
    assert stats == {"checked": 4, "expired": 1, "paid": 1, "deferred": 2, "skipped": 0}
    mock_stripe.expire_checkout_session.assert_called_once_with("cs_rec_exp_451")

    db_session.refresh(a1)
    db_session.refresh(a2)
    db_session.refresh(a3)
    db_session.refresh(a4)
    assert a1.status == "expired"
    assert a2.status == "open"
    assert a3.status == "succeeded"
    assert a4.status == "processing"

    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=451, event_type="payment.expired")
        .count()
        == 1
    )
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=453, event_type="payment.succeeded")
        .count()
        == 1
    )


def test_reserve_expired_with_lost_stripe_response_initiated_preserves_reserve(
    db_session: Session,
) -> None:
    """Потерян Stripe отговор + изтекъл резерв: и двата пътя (cancel reserve_expired и reconcile) пазят initiated."""
    now = datetime.datetime.now(datetime.UTC)
    past = now - datetime.timedelta(minutes=20)
    stripe_created: dict[str, Any] = {}

    def _drop_after_create(**kwargs: Any) -> Any:
        key = kwargs["idempotency_key"]
        stripe_created[key] = SimpleNamespace(
            id="cs_lost_expired_501",
            url="https://checkout.stripe.com/pay/cs_lost_expired_501",
        )
        raise TimeoutError("Response lost after Stripe created session")

    mock_client = MagicMock(spec=StripeClient)
    mock_client.create_checkout_session.side_effect = _drop_after_create
    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        r_create = client.post(
            "/internal/payments/checkout-sessions",
            json={
                "order_id": 501,
                "attempt_key": "order_501_attempt_1",
                "amount_minor": 4000,
                "currency": "EUR",
                "line_items": [{"name": "Item", "unit_amount": 4000, "quantity": 1}],
                "order_expires_at": past.isoformat(),
            },
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r_create.status_code == 502
        assert "order_501_attempt_1" in stripe_created

        # Път 1: /cancel с reason='reserve_expired' -> НЕ връща already_expired и НЕ прекратява initiated
        r_exp = client.post(
            "/internal/payments/orders/501/cancel",
            json={"reason": "reserve_expired", "order_expires_at": past.isoformat()},
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r_exp.status_code == 409
        assert r_exp.json()["detail"]["current_status"] == "initiated"

        # Път 2: reconcile_unresolved_payments при изтекъл TTL -> остава 'deferred', без payment.expired
        svc = PaymentService(
            repository=PaymentRepository(db_session),
            session=db_session,
            stripe_client=mock_client,
        )
        rec_stats = svc.reconcile_unresolved_payments(now=now)
        assert rec_stats == {
            "checked": 1,
            "expired": 0,
            "paid": 0,
            "deferred": 1,
            "skipped": 0,
        }

        attempt = db_session.query(PaymentAttempt).filter_by(order_id=501).one()
        assert attempt.status == "initiated"
        assert (
            db_session.query(PaymentOutboxEvent)
            .filter_by(order_id=501, event_type="payment.expired")
            .count()
            == 0
        )

        # По-късно пристига истинският webhook за cs_lost_expired_501 -> плащането успява нормално
        wh_payload = json.dumps(
            {
                "id": "evt_lost_recovered_501",
                "type": "checkout.session.completed",
                "livemode": False,
                "data": {
                    "object": {
                        "id": "cs_lost_expired_501",
                        "livemode": False,
                        "payment_status": "paid",
                        "amount_total": 4000,
                        "currency": "eur",
                        "client_reference_id": "501",
                        "metadata": {
                            "order_id": "501",
                            "payment_attempt_id": str(attempt.id),
                            "attempt_key": "order_501_attempt_1",
                        },
                    }
                },
            }
        ).encode()
        app.dependency_overrides.pop(get_stripe_client, None)
        with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
            r_wh = client.post(
                "/webhooks/stripe",
                content=wh_payload,
                headers={
                    "Stripe-Signature": generate_stripe_signature(
                        wh_payload, TEST_WEBHOOK_SECRET
                    )
                },
            )
            assert r_wh.status_code == 200
            assert r_wh.json()["status"] == "success"

        db_session.refresh(attempt)
        assert attempt.status == "succeeded"
        assert (
            db_session.query(PaymentOutboxEvent)
            .filter_by(order_id=501, event_type="payment.succeeded")
            .count()
            == 1
        )
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


@pytest.mark.parametrize(
    ("bad_session", "expected_reason"),
    [
        (
            SimpleNamespace(
                id="cs_disc_base",
                livemode=False,
                status="complete",
                payment_status="paid",
                amount_total=9999,
                currency="eur",
                client_reference_id=None,
                metadata=None,
            ),
            "amount_mismatch",
        ),
        (
            SimpleNamespace(
                id="cs_disc_base",
                livemode=False,
                status="complete",
                payment_status="paid",
                amount_total=5000,
                currency="usd",
                client_reference_id=None,
                metadata=None,
            ),
            "currency_mismatch",
        ),
        (
            SimpleNamespace(
                id="cs_disc_base",
                livemode=True,
                status="complete",
                payment_status="paid",
                amount_total=5000,
                currency="eur",
                client_reference_id=None,
                metadata=None,
            ),
            "mode_mismatch",
        ),
        (
            SimpleNamespace(
                id="cs_foreign_999",
                livemode=False,
                status="complete",
                payment_status="paid",
                amount_total=5000,
                currency="eur",
                client_reference_id=None,
                metadata=None,
            ),
            "session_id_mismatch",
        ),
    ],
)
def test_reconcile_and_cancel_conflict_reject_mismatched_paid_and_record_discrepancy(
    db_session: Session,
    bad_session: SimpleNamespace,
    expected_reason: str,
) -> None:
    """Сверка и конфликт на отмяна отхвърлят невалидна сума/валута/режим/чужда сесия и записват discrepancy."""
    att = PaymentAttempt(
        order_id=520,
        attempt_key="order_520_a1",
        status="open",
        amount_minor=5000,
        currency="EUR",
        mode="test",
    )
    db_session.add(att)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=att.id,
            order_id=520,
            status="open",
            amount_minor=5000,
            currency="EUR",
            mode="test",
            stripe_session_id="cs_disc_base",
            checkout_url="https://checkout.stripe.com/pay/cs_disc_base",
            idempotency_key="order_520_a1",
        )
    )
    db_session.commit()

    mock_stripe = MagicMock(spec=StripeClient)
    mock_stripe.retrieve_checkout_session.return_value = bad_session
    mock_stripe.expire_checkout_session.side_effect = RuntimeError(
        "Cannot expire a Checkout Session whose status is complete"
    )

    # 1. Сверка -> deferred + PaymentDiscrepancy(source='reconcile')
    svc = PaymentService(
        repository=PaymentRepository(db_session),
        session=db_session,
        stripe_client=mock_stripe,
    )
    rec_stats = svc.reconcile_unresolved_payments()
    assert rec_stats["paid"] == 0
    assert rec_stats["deferred"] == 1
    db_session.refresh(att)
    assert att.status == "open"
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=520, event_type="payment.succeeded")
        .count()
        == 0
    )
    d_rec = (
        db_session.query(PaymentDiscrepancy)
        .filter_by(order_id=520, source="reconcile")
        .one()
    )
    assert d_rec.reason == expected_reason

    # 2. Конфликт при отмяна -> 409 discrepancy (не плаща и не отменя) + PaymentDiscrepancy(source='cancel_conflict')
    app.dependency_overrides[get_stripe_client] = lambda: mock_stripe
    try:
        r_cancel = client.post(
            "/internal/payments/orders/520/cancel",
            json={"reason": "cancel"},
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r_cancel.status_code == 409
        assert r_cancel.json()["detail"]["current_status"] == "discrepancy"
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)

    db_session.refresh(att)
    assert att.status == "open"
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=520, event_type="payment.succeeded")
        .count()
        == 0
    )
    d_cancel = (
        db_session.query(PaymentDiscrepancy)
        .filter_by(order_id=520, source="cancel_conflict")
        .one()
    )
    assert d_cancel.reason == expected_reason
