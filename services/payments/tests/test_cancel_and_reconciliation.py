"""Тестове за /internal/payments/orders/{id}/cancel и сверка със Stripe (reconciliation)."""

import datetime
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.dependencies import get_stripe_client
from app.main import app
from app.models import (
    PaymentAttempt,
    PaymentDiscrepancy,
    PaymentOrderClosure,
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


def test_reconcile_ignores_stale_complete_unpaid_when_webhook_recorded_failed(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Сценарий 1а: докато reconcile чака Stripe, webhook записва failed; старият complete/unpaid не го презаписва с processing."""
    attempt = PaymentAttempt(
        order_id=530,
        attempt_key="order_530_attempt_1",
        status="open",
        amount_minor=4200,
        currency="EUR",
    )
    db_session.add(attempt)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=attempt.id,
            order_id=530,
            status="open",
            amount_minor=4200,
            currency="EUR",
            stripe_session_id="cs_stale_rec_530",
            idempotency_key="order_530_attempt_1",
        )
    )
    db_session.commit()

    entered_stripe = threading.Event()
    allow_stripe_return = threading.Event()

    def _slow_retrieve(sid: str) -> Any:
        assert sid == "cs_stale_rec_530"
        entered_stripe.set()
        assert allow_stripe_return.wait(timeout=5.0)
        return SimpleNamespace(
            id="cs_stale_rec_530",
            livemode=False,
            status="complete",
            payment_status="unpaid",
        )

    mock_stripe = MagicMock(spec=StripeClient)
    mock_stripe.retrieve_checkout_session.side_effect = _slow_retrieve

    def _run_reconcile() -> str:
        with session_factory() as rec_session:
            svc = PaymentService(
                repository=PaymentRepository(rec_session),
                session=rec_session,
                stripe_client=mock_stripe,
            )
            return svc.reconcile_single_attempt(
                attempt.id, datetime.datetime.now(datetime.UTC)
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        rec_future = pool.submit(_run_reconcile)
        assert entered_stripe.wait(timeout=5.0)

        # Докато reconcile чака Stripe (без DB lock), webhook записва async_payment_failed
        wh_body = json.dumps(
            {
                "id": "evt_stale_rec_fail_530",
                "type": "checkout.session.async_payment_failed",
                "livemode": False,
                "data": {
                    "object": {
                        "id": "cs_stale_rec_530",
                        "livemode": False,
                        "payment_status": "unpaid",
                        "client_reference_id": "530",
                        "metadata": {
                            "order_id": "530",
                            "attempt_key": "order_530_attempt_1",
                            "payment_attempt_id": str(attempt.id),
                        },
                    }
                },
            }
        ).encode("utf-8")
        with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
            r_wh = client.post(
                "/webhooks/stripe",
                content=wh_body,
                headers={
                    "Stripe-Signature": generate_stripe_signature(
                        wh_body, TEST_WEBHOOK_SECRET
                    )
                },
            )
        assert r_wh.status_code == 200
        assert r_wh.json()["status"] == "failed"

        allow_stripe_return.set()
        rec_future.result(timeout=5.0)

    db_session.expire_all()
    db_session.refresh(attempt)
    assert attempt.status == "failed"
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=530, event_type="payment.failed")
        .count()
        == 1
    )
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=530, event_type="payment.processing")
        .count()
        == 0
    )


def test_cancel_conflict_ignores_stale_complete_unpaid_when_webhook_recorded_succeeded(
    db_session: Session,
) -> None:
    """Сценарий 1б: докато cancel сверява конфликт със Stripe, webhook записва succeeded; старият complete/unpaid не го презаписва с processing."""
    attempt = PaymentAttempt(
        order_id=531,
        attempt_key="order_531_attempt_1",
        status="open",
        amount_minor=4800,
        currency="EUR",
    )
    db_session.add(attempt)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=attempt.id,
            order_id=531,
            status="open",
            amount_minor=4800,
            currency="EUR",
            stripe_session_id="cs_stale_cancel_531",
            idempotency_key="order_531_attempt_1",
        )
    )
    db_session.commit()

    entered_retrieve = threading.Event()
    allow_retrieve_return = threading.Event()

    mock_stripe = MagicMock(spec=StripeClient)
    mock_stripe.construct_webhook_event.side_effect = StripeClient(
        api_key="sk_test_unused"
    ).construct_webhook_event
    mock_stripe.expire_checkout_session.side_effect = RuntimeError(
        "Cannot expire Checkout Session whose status is complete"
    )

    def _slow_retrieve(sid: str) -> Any:
        assert sid == "cs_stale_cancel_531"
        entered_retrieve.set()
        assert allow_retrieve_return.wait(timeout=5.0)
        return SimpleNamespace(
            id="cs_stale_cancel_531",
            livemode=False,
            status="complete",
            payment_status="unpaid",
        )

    mock_stripe.retrieve_checkout_session.side_effect = _slow_retrieve

    app.dependency_overrides[get_stripe_client] = lambda: mock_stripe
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            cancel_future = pool.submit(
                lambda: client.post(
                    "/internal/payments/orders/531/cancel",
                    json={"reason": "cancel"},
                    headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
                )
            )
            assert entered_retrieve.wait(timeout=5.0)

            # Без задържан DB lock webhook спокойно записва succeeded
            wh_body = json.dumps(
                {
                    "id": "evt_stale_cancel_paid_531",
                    "type": "checkout.session.async_payment_succeeded",
                    "livemode": False,
                    "data": {
                        "object": {
                            "id": "cs_stale_cancel_531",
                            "livemode": False,
                            "payment_status": "paid",
                            "amount_total": 4800,
                            "currency": "eur",
                            "client_reference_id": "531",
                            "metadata": {
                                "order_id": "531",
                                "attempt_key": "order_531_attempt_1",
                                "payment_attempt_id": str(attempt.id),
                            },
                        }
                    },
                }
            ).encode("utf-8")
            with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
                r_wh = client.post(
                    "/webhooks/stripe",
                    content=wh_body,
                    headers={
                        "Stripe-Signature": generate_stripe_signature(
                            wh_body, TEST_WEBHOOK_SECRET
                        )
                    },
                )
            assert r_wh.status_code == 200
            assert r_wh.json()["status"] == "success"

            allow_retrieve_return.set()
            r_cancel = cancel_future.result(timeout=5.0)
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)

    assert r_cancel.status_code == 409
    assert r_cancel.json()["detail"]["code"] == "ORDER_ALREADY_PAID"
    db_session.expire_all()
    db_session.refresh(attempt)
    assert attempt.status == "succeeded"
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=531, event_type="payment.succeeded")
        .count()
        == 1
    )
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=531, event_type="payment.processing")
        .count()
        == 0
    )


def test_reconcile_initiated_without_session_within_window_expires_before_stock_restore(
    db_session: Session,
) -> None:
    """Сценарий 2 (в безопасното окно): не дошлата до Stripe или потеряла отговор initiated сесия се изяснява и затваря преди payment.expired."""
    now = datetime.datetime.now(datetime.UTC)
    past = now - datetime.timedelta(minutes=25)
    line_items = [
        {
            "price_data": {
                "currency": "eur",
                "unit_amount": 3000,
                "product_data": {"name": "Lost Item"},
            },
            "quantity": 1,
        }
    ]

    # Случай А: първият повик никога не е стигнал до Stripe -> сверката го повтаря със същия ключ/параметри
    att_never_reached = PaymentAttempt(
        order_id=540,
        attempt_key="order_540_attempt_1",
        status="initiated",
        amount_minor=3000,
        currency="EUR",
        line_items=line_items,
        order_expires_at=past,
        created_at=now - datetime.timedelta(minutes=30),
    )
    db_session.add(att_never_reached)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=att_never_reached.id,
            order_id=540,
            status="initiated",
            amount_minor=3000,
            currency="EUR",
            idempotency_key="order_540_attempt_1",
        )
    )

    # Случай Б: Stripe вече е създал отворена сесия по този ключ, но отговорът се е изгубил -> същият ключ я възстановява и я затваря преди payment.expired
    att_lost_open = PaymentAttempt(
        order_id=541,
        attempt_key="order_541_attempt_1",
        status="initiated",
        amount_minor=3500,
        currency="EUR",
        line_items=line_items,
        order_expires_at=past,
        created_at=now - datetime.timedelta(minutes=30),
    )
    db_session.add(att_lost_open)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=att_lost_open.id,
            order_id=541,
            status="initiated",
            amount_minor=3500,
            currency="EUR",
            idempotency_key="order_541_attempt_1",
        )
    )
    db_session.commit()

    stripe_sessions: dict[str, dict[str, Any]] = {
        "order_541_attempt_1": {
            "id": "cs_recovered_open_541",
            "url": "https://checkout.stripe.com/pay/cs_recovered_open_541",
            "status": "open",
            "payment_status": "unpaid",
        }
    }
    captured_calls: dict[str, dict[str, Any]] = {}
    expire_order_checks: list[tuple[str, int]] = []

    mock_stripe = MagicMock(spec=StripeClient)

    def _create_session(**kwargs: Any) -> Any:
        key = kwargs["idempotency_key"]
        captured_calls[key] = dict(kwargs)
        existing = stripe_sessions.get(key)
        if existing is None:
            sid = f"cs_created_rec_{len(stripe_sessions) + 1}"
            existing = {
                "id": sid,
                "url": f"https://checkout.stripe.com/pay/{sid}",
                "status": "open",
                "payment_status": "unpaid",
            }
            stripe_sessions[key] = existing
        return SimpleNamespace(**existing)

    def _retrieve_session(sid: str) -> Any:
        for data in stripe_sessions.values():
            if data["id"] == sid:
                return SimpleNamespace(**data, livemode=False)
        raise AssertionError(f"Unknown session {sid}")

    def _expire_session(sid: str) -> Any:
        for key, data in stripe_sessions.items():
            if data["id"] == sid:
                order_num = int(key.split("_")[1])
                outbox_count = (
                    db_session.query(PaymentOutboxEvent)
                    .filter_by(order_id=order_num, event_type="payment.expired")
                    .count()
                )
                expire_order_checks.append((sid, outbox_count))
                data["status"] = "expired"
                return SimpleNamespace(**data)
        raise AssertionError(f"Unknown session to expire {sid}")

    mock_stripe.create_checkout_session.side_effect = _create_session
    mock_stripe.retrieve_checkout_session.side_effect = _retrieve_session
    mock_stripe.expire_checkout_session.side_effect = _expire_session

    service = PaymentService(
        repository=PaymentRepository(db_session),
        session=db_session,
        stripe_client=mock_stripe,
    )
    stats = service.reconcile_unresolved_payments(now=now)
    assert stats == {"checked": 2, "expired": 2, "paid": 0, "deferred": 0, "skipped": 0}

    assert captured_calls["order_540_attempt_1"]["line_items"] == line_items
    assert captured_calls["order_540_attempt_1"]["client_reference_id"] == "540"
    assert captured_calls["order_541_attempt_1"]["client_reference_id"] == "541"
    # Закриването в Stripe е станало ПРЕДИ запис на payment.expired в outbox!
    assert len(expire_order_checks) == 2
    assert all(outbox_cnt == 0 for _, outbox_cnt in expire_order_checks)
    assert stripe_sessions["order_540_attempt_1"]["status"] == "expired"
    assert stripe_sessions["order_541_attempt_1"]["status"] == "expired"

    db_session.expire_all()
    db_session.refresh(att_never_reached)
    db_session.refresh(att_lost_open)
    assert att_never_reached.status == "expired"
    assert att_lost_open.status == "expired"
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=540, event_type="payment.expired")
        .count()
        == 1
    )
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=541, event_type="payment.expired")
        .count()
        == 1
    )


def test_reconcile_initiated_without_session_outside_window_records_discrepancy(
    db_session: Session,
) -> None:
    """Сценарий 2 (извън безопасното окно): не се създава нова Stripe сесия и случаят се записва за разбор."""
    now = datetime.datetime.now(datetime.UTC)
    too_old = now - datetime.timedelta(
        seconds=settings.stripe_idempotency_window_seconds + 3600
    )
    attempt = PaymentAttempt(
        order_id=542,
        attempt_key="order_542_attempt_1",
        status="initiated",
        amount_minor=5000,
        currency="EUR",
        order_expires_at=now - datetime.timedelta(hours=24),
        created_at=too_old,
    )
    db_session.add(attempt)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=attempt.id,
            order_id=542,
            status="initiated",
            amount_minor=5000,
            currency="EUR",
            idempotency_key="order_542_attempt_1",
            created_at=too_old,
        )
    )
    db_session.commit()

    mock_stripe = MagicMock(spec=StripeClient)
    service = PaymentService(
        repository=PaymentRepository(db_session),
        session=db_session,
        stripe_client=mock_stripe,
    )

    stats1 = service.reconcile_unresolved_payments(now=now)
    stats2 = service.reconcile_unresolved_payments(now=now)
    assert stats1 == {
        "checked": 1,
        "expired": 0,
        "paid": 0,
        "deferred": 1,
        "skipped": 0,
    }
    assert stats2 == {
        "checked": 1,
        "expired": 0,
        "paid": 0,
        "deferred": 1,
        "skipped": 0,
    }

    mock_stripe.create_checkout_session.assert_not_called()
    mock_stripe.expire_checkout_session.assert_not_called()

    db_session.refresh(attempt)
    assert attempt.status == "initiated"
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=542, event_type="payment.expired")
        .count()
        == 0
    )
    discrepancies = (
        db_session.query(PaymentDiscrepancy)
        .filter_by(
            order_id=542, source="reconcile", reason="idempotency_window_expired"
        )
        .all()
    )
    assert len(discrepancies) == 1


def test_old_initiated_attempt_blocks_stripe_create_on_checkout_and_reconcile_deduplicates_discrepancy(
    db_session: Session,
) -> None:
    """Единна проверка на безопасния срок: старият initiated не вика Stripe create нито при checkout, нито при сверка."""
    now = datetime.datetime.now(datetime.UTC)
    too_old = now - datetime.timedelta(
        seconds=settings.stripe_idempotency_window_seconds + 3600
    )
    attempt = PaymentAttempt(
        order_id=543,
        attempt_key="order_543_attempt_1",
        status="initiated",
        amount_minor=5100,
        currency="EUR",
        order_expires_at=now + datetime.timedelta(minutes=15),
        created_at=too_old,
    )
    db_session.add(attempt)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=attempt.id,
            order_id=543,
            status="initiated",
            amount_minor=5100,
            currency="EUR",
            idempotency_key="order_543_attempt_1",
            created_at=too_old,
        )
    )
    db_session.commit()

    mock_stripe = MagicMock(spec=StripeClient)
    app.dependency_overrides[get_stripe_client] = lambda: mock_stripe
    try:
        # 1. Обикновеният checkout отказва старата initiated-попытка без Stripe повик
        r_co = client.post(
            "/internal/payments/checkout-sessions",
            json={
                "order_id": 543,
                "attempt_key": "order_543_attempt_1",
                "amount_minor": 5100,
                "currency": "EUR",
                "line_items": [{"name": "Item", "unit_amount": 5100, "quantity": 1}],
            },
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r_co.status_code == 409
        assert r_co.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
        assert r_co.json()["detail"]["current_status"] == "discrepancy"
        mock_stripe.create_checkout_session.assert_not_called()

        # 2. Повторните фонови сверки също не викат Stripe create и НЕ дублират записа за разбор
        svc = PaymentService(
            repository=PaymentRepository(db_session),
            session=db_session,
            stripe_client=mock_stripe,
        )
        assert svc.reconcile_unresolved_payments(now=now) == {
            "checked": 1,
            "expired": 0,
            "paid": 0,
            "deferred": 1,
            "skipped": 0,
        }
        assert svc.reconcile_unresolved_payments(now=now) == {
            "checked": 1,
            "expired": 0,
            "paid": 0,
            "deferred": 1,
            "skipped": 0,
        }
        mock_stripe.create_checkout_session.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)

    discrepancies = (
        db_session.query(PaymentDiscrepancy)
        .filter_by(order_id=543, reason="idempotency_window_expired")
        .all()
    )
    assert len(discrepancies) == 1
    assert discrepancies[0].source == "checkout"


def test_interrupted_cancel_recovers_before_and_after_stripe_expire_and_preserves_paid(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Прекъсната отмяна (преди Stripe expire, след Stripe expire и при спечелило плащане) се възстановява без timer unlock."""

    class SimulatedProcessCrash(BaseException):
        pass

    now = datetime.datetime.now(datetime.UTC)

    # --- Момент 1: рестарт след запис cancelling в БД, ПРЕДИ expire в Stripe ---
    a1 = PaymentAttempt(
        order_id=550,
        attempt_key="order_550_attempt_1",
        status="open",
        amount_minor=4000,
        currency="EUR",
    )
    db_session.add(a1)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a1.id,
            order_id=550,
            status="open",
            amount_minor=4000,
            currency="EUR",
            stripe_session_id="cs_crash_before_550",
            checkout_url="https://checkout.stripe.com/pay/cs_crash_before_550",
            idempotency_key="order_550_attempt_1",
        )
    )
    db_session.commit()

    stripe_state: dict[str, dict[str, Any]] = {
        "cs_crash_before_550": {
            "id": "cs_crash_before_550",
            "status": "open",
            "payment_status": "unpaid",
            "livemode": False,
        },
        "cs_crash_after_551": {
            "id": "cs_crash_after_551",
            "status": "open",
            "payment_status": "unpaid",
            "livemode": False,
        },
        "cs_crash_paid_552": {
            "id": "cs_crash_paid_552",
            "status": "complete",
            "payment_status": "paid",
            "livemode": False,
            "amount_total": 6000,
            "currency": "eur",
            "client_reference_id": "552",
            "metadata": {"order_id": "552", "attempt_key": "order_552_attempt_1"},
        },
    }

    mock_crash_1 = MagicMock(spec=StripeClient)
    mock_crash_1.expire_checkout_session.side_effect = SimulatedProcessCrash(
        "kill before stripe expire"
    )
    app.dependency_overrides[get_stripe_client] = lambda: mock_crash_1
    try:
        try:
            client.post(
                "/internal/payments/orders/550/cancel",
                json={"reason": "cancel"},
                headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
            )
            raise AssertionError("Expected SimulatedProcessCrash")
        except SimulatedProcessCrash:
            pass
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)

    assert stripe_state["cs_crash_before_550"]["status"] == "open"
    closure_550 = db_session.query(PaymentOrderClosure).filter_by(order_id=550).one()
    assert closure_550.status == "cancelling"

    # Докато изходът не е установен: нов checkout е забранен (409), а таймаут на Stripe не сваля блокировката дори след +48ч
    mock_blocked = MagicMock(spec=StripeClient)
    mock_blocked.retrieve_checkout_session.side_effect = TimeoutError("Stripe offline")
    app.dependency_overrides[get_stripe_client] = lambda: mock_blocked
    try:
        r_blocked_co = client.post(
            "/internal/payments/checkout-sessions",
            json={
                "order_id": 550,
                "attempt_key": "order_550_attempt_2",
                "amount_minor": 4000,
                "currency": "EUR",
                "line_items": [{"name": "Item", "unit_amount": 4000, "quantity": 1}],
            },
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r_blocked_co.status_code == 409
        assert r_blocked_co.json()["detail"]["current_status"] == "cancelling"
        mock_blocked.create_checkout_session.assert_not_called()

        r_reserve = client.post(
            "/internal/payments/orders/550/cancel",
            json={"reason": "reserve_expired"},
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r_reserve.status_code == 409

        with session_factory() as s_down:
            svc_down = PaymentService(
                repository=PaymentRepository(s_down),
                session=s_down,
                stripe_client=mock_blocked,
            )
            stats_down = svc_down.reconcile_unresolved_payments(
                now=now + datetime.timedelta(hours=48)
            )
            assert stats_down["deferred"] == 1
            assert stats_down["expired"] == 0
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)

    db_session.expire_all()
    assert (
        db_session.query(PaymentOrderClosure).filter_by(order_id=550).one().status
        == "cancelling"
    )

    # След рестарт Stripe отговаря -> сверката извиква expire и финализира отмяната
    expire_calls: list[str] = []
    mock_recover = MagicMock(spec=StripeClient)
    mock_recover.retrieve_checkout_session.side_effect = lambda sid: SimpleNamespace(
        **stripe_state[sid]
    )

    def _mock_expire(sid: str) -> Any:
        expire_calls.append(sid)
        stripe_state[sid]["status"] = "expired"
        return SimpleNamespace(**stripe_state[sid])

    mock_recover.expire_checkout_session.side_effect = _mock_expire

    with session_factory() as s_restart:
        svc_restart = PaymentService(
            repository=PaymentRepository(s_restart),
            session=s_restart,
            stripe_client=mock_recover,
        )
        stats_1 = svc_restart.reconcile_unresolved_payments(now=now)
        assert stats_1 == {
            "checked": 1,
            "expired": 1,
            "paid": 0,
            "deferred": 0,
            "skipped": 0,
        }
        stats_restart_repeat = svc_restart.reconcile_unresolved_payments(now=now)
        assert stats_restart_repeat["checked"] == 0

    assert expire_calls == ["cs_crash_before_550"]
    assert stripe_state["cs_crash_before_550"]["status"] == "expired"
    db_session.expire_all()
    assert (
        db_session.query(PaymentOrderClosure).filter_by(order_id=550).one().status
        == "cancelled"
    )
    assert (
        db_session.query(PaymentAttempt).filter_by(order_id=550).one().status
        == "expired"
    )
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=550, event_type="payment.expired")
        .count()
        == 1
    )

    # --- Момент 2: рестарт СЛЕД успешен expire в Stripe, но ПРЕДИ запис в БД ---
    a2 = PaymentAttempt(
        order_id=551,
        attempt_key="order_551_attempt_1",
        status="open",
        amount_minor=4500,
        currency="EUR",
    )
    db_session.add(a2)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a2.id,
            order_id=551,
            status="open",
            amount_minor=4500,
            currency="EUR",
            stripe_session_id="cs_crash_after_551",
            checkout_url="https://checkout.stripe.com/pay/cs_crash_after_551",
            idempotency_key="order_551_attempt_1",
        )
    )
    db_session.commit()

    mock_crash_2 = MagicMock(spec=StripeClient)
    mock_crash_2.expire_checkout_session.side_effect = _mock_expire
    app.dependency_overrides[get_stripe_client] = lambda: mock_crash_2
    try:
        with patch.object(
            PaymentRepository,
            "finalize_order_closure",
            side_effect=SimulatedProcessCrash("kill before Phase 3 DB write"),
        ):
            try:
                client.post(
                    "/internal/payments/orders/551/cancel",
                    json={"reason": "cancel"},
                    headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
                )
                raise AssertionError("Expected SimulatedProcessCrash")
            except SimulatedProcessCrash:
                pass
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)

    assert stripe_state["cs_crash_after_551"]["status"] == "expired"
    db_session.expire_all()
    assert (
        db_session.query(PaymentOrderClosure).filter_by(order_id=551).one().status
        == "cancelling"
    )

    expire_calls.clear()
    with session_factory() as s_restart_2:
        svc_2 = PaymentService(
            repository=PaymentRepository(s_restart_2),
            session=s_restart_2,
            stripe_client=mock_recover,
        )
        stats_2 = svc_2.reconcile_unresolved_payments(now=now)
        assert stats_2 == {
            "checked": 1,
            "expired": 1,
            "paid": 0,
            "deferred": 0,
            "skipped": 0,
        }

    # Stripe вече е expired -> сверката НЕ вика expire повторно и финализира отмяната
    assert expire_calls == []
    db_session.expire_all()
    assert (
        db_session.query(PaymentOrderClosure).filter_by(order_id=551).one().status
        == "cancelled"
    )
    assert (
        db_session.query(PaymentAttempt).filter_by(order_id=551).one().status
        == "expired"
    )

    # Повторен cancel минава идемпотентно (can_cancel), а нов checkout остава отказан
    app.dependency_overrides[get_stripe_client] = lambda: mock_recover
    try:
        for oid_done in (550, 551):
            r_cancel_done = client.post(
                f"/internal/payments/orders/{oid_done}/cancel",
                json={"reason": "cancel"},
                headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
            )
            assert r_cancel_done.status_code == 200
            assert r_cancel_done.json()["status"] == "can_cancel"

            r_new_co = client.post(
                "/internal/payments/checkout-sessions",
                json={
                    "order_id": oid_done,
                    "attempt_key": f"order_{oid_done}_attempt_2",
                    "amount_minor": 4000,
                    "currency": "EUR",
                    "line_items": [
                        {"name": "Item", "unit_amount": 4000, "quantity": 1}
                    ],
                },
                headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
            )
            assert r_new_co.status_code == 409
        mock_recover.create_checkout_session.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)

    # --- Подтверждённая оплата не превращается в отмену ---
    a3 = PaymentAttempt(
        order_id=552,
        attempt_key="order_552_attempt_1",
        status="cancelling",
        amount_minor=6000,
        currency="EUR",
    )
    db_session.add(a3)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=a3.id,
            order_id=552,
            status="cancelling",
            amount_minor=6000,
            currency="EUR",
            stripe_session_id="cs_crash_paid_552",
            idempotency_key="order_552_attempt_1",
        )
    )
    db_session.add(
        PaymentOrderClosure(order_id=552, status="cancelling", reason="cancel")
    )
    db_session.commit()

    with session_factory() as s_paid:
        svc_paid = PaymentService(
            repository=PaymentRepository(s_paid),
            session=s_paid,
            stripe_client=mock_recover,
        )
        stats_paid = svc_paid.reconcile_unresolved_payments(now=now)
        assert stats_paid == {
            "checked": 1,
            "expired": 0,
            "paid": 1,
            "deferred": 0,
            "skipped": 0,
        }

    db_session.expire_all()
    assert db_session.query(PaymentOrderClosure).filter_by(order_id=552).first() is None
    assert (
        db_session.query(PaymentAttempt).filter_by(order_id=552).one().status
        == "succeeded"
    )
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=552, event_type="payment.succeeded")
        .count()
        == 1
    )
    assert (
        db_session.query(PaymentOutboxEvent)
        .filter_by(order_id=552, event_type="payment.expired")
        .count()
        == 0
    )
