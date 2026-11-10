"""Тестове за /internal/payments/orders/{id}/cancel и сверка със Stripe (reconciliation)."""

import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.dependencies import get_stripe_client
from app.main import app
from app.models import PaymentAttempt, PaymentOutboxEvent, StripeCheckoutSession
from app.repository import PaymentRepository
from app.service import PaymentService
from app.stripe_client import StripeClient
from tests.helpers import TEST_INTERNAL_TOKEN

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
        status="complete",
        payment_status="paid",
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
            return SimpleNamespace(status="open", payment_status="unpaid")
        if sid == "cs_rec_down_452":
            raise TimeoutError("Stripe timeout")
        if sid == "cs_rec_paid_453":
            return SimpleNamespace(status="complete", payment_status="paid")
        if sid == "cs_rec_proc_454":
            return SimpleNamespace(status="complete", payment_status="unpaid")
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
