"""Тестове на /internal/payments/checkout-sessions: защита, конкурентност, загуба на отговор и нови опити."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.dependencies import get_stripe_client
from app.main import app
from app.models import PaymentAttempt, StripeCheckoutSession
from app.stripe_client import StripeClient
from tests.helpers import (
    TEST_INTERNAL_TOKEN,
    TEST_WEBHOOK_SECRET,
    build_internal_checkout_payload,
    generate_stripe_signature,
)

client = TestClient(app)


def test_internal_checkout_session_requires_valid_internal_token() -> None:
    """Произволен външен клиент без валиден X-Internal-Token получава 401."""
    payload = build_internal_checkout_payload(order_id=1)

    resp_missing = client.post("/internal/payments/checkout-sessions", json=payload)
    assert resp_missing.status_code == 401
    assert resp_missing.json()["detail"]["code"] == "INVALID_INTERNAL_TOKEN"

    resp_wrong = client.post(
        "/internal/payments/checkout-sessions",
        json=payload,
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp_wrong.status_code == 401

    resp_cancel_wrong = client.post(
        "/internal/payments/orders/1/cancel",
        json={"reason": "cancel"},
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp_cancel_wrong.status_code == 401
    assert resp_cancel_wrong.json()["detail"]["code"] == "INVALID_INTERNAL_TOKEN"


def test_cancel_without_attempt_blocks_delayed_checkout_session(
    db_session: Session,
) -> None:
    """Ако cancel е потвърден преди да има опит, закъснял checkout не може да създаде Stripe сесия."""
    cancel_resp = client.post(
        "/internal/payments/orders/95/cancel",
        json={"reason": "cancel"},
        headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "can_cancel"

    mock_client = MagicMock(spec=StripeClient)
    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        late_checkout = client.post(
            "/internal/payments/checkout-sessions",
            json=build_internal_checkout_payload(order_id=95),
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert late_checkout.status_code == 409
        assert late_checkout.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
        assert late_checkout.json()["detail"]["current_status"] == "cancelled"
        assert mock_client.create_checkout_session.call_count == 0
        assert db_session.query(PaymentAttempt).filter_by(order_id=95).count() == 0
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_checkout_session_amount_limits_and_idempotent_reuse(
    db_session: Session,
) -> None:
    """Валидация на сумата и повторно извикване с връщане на същата отворена сесия."""
    resp_low = client.post(
        "/internal/payments/checkout-sessions",
        json=build_internal_checkout_payload(order_id=10, amount_minor=49),
        headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
    )
    assert resp_low.status_code == 400
    assert resp_low.json()["detail"]["code"] == "INVALID_ORDER_AMOUNT"

    mock_client = MagicMock(spec=StripeClient)
    mock_client.create_checkout_session.return_value = SimpleNamespace(
        id="cs_reuse_1",
        url="https://checkout.stripe.com/pay/cs_reuse_1",
    )
    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        r1 = client.post(
            "/internal/payments/checkout-sessions",
            json=build_internal_checkout_payload(order_id=11),
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r1.status_code == 200
        assert r1.json()["session_id"] == "cs_reuse_1"
        assert mock_client.create_checkout_session.call_count == 1

        # Повторен запрос -> връща съхранената сесия без ново обръщение към Stripe
        r2 = client.post(
            "/internal/payments/checkout-sessions",
            json=build_internal_checkout_payload(order_id=11),
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r2.status_code == 200
        assert r2.json() == r1.json()
        assert mock_client.create_checkout_session.call_count == 1
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_checkout_session_concurrent_requests_create_single_attempt_and_stripe_session(
    db_session: Session,
) -> None:
    """Две паралелни заявки връщат същия URL и създават точно един опит/сесия в Stripe и БД."""
    created_by_idempotency_key: dict[str, Any] = {}

    mock_client = MagicMock(spec=StripeClient)

    def _idempotent_create(**kwargs: Any) -> Any:
        key = kwargs["idempotency_key"]
        if key not in created_by_idempotency_key:
            created_by_idempotency_key[key] = SimpleNamespace(
                id="cs_conc_pay_1",
                url="https://checkout.stripe.com/pay/cs_conc_pay_1",
            )
        return created_by_idempotency_key[key]

    mock_client.create_checkout_session.side_effect = _idempotent_create
    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        payload = build_internal_checkout_payload(
            order_id=25, attempt_key="order_25_a1"
        )

        def _call() -> tuple[int, dict[str, Any]]:
            tc = TestClient(app)
            r = tc.post(
                "/internal/payments/checkout-sessions",
                json=payload,
                headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
            )
            return r.status_code, r.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_call)
            f2 = pool.submit(_call)
            c1, d1 = f1.result()
            c2, d2 = f2.result()

        assert c1 == 200 and c2 == 200
        assert d1 == d2
        assert len(created_by_idempotency_key) == 1

        attempts_count = db_session.execute(
            text("SELECT count(*) FROM payment_attempts WHERE order_id = 25")
        ).scalar()
        sessions_count = db_session.execute(
            text("SELECT count(*) FROM stripe_checkout_sessions WHERE order_id = 25")
        ).scalar()
        assert attempts_count == 1
        assert sessions_count == 1
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_checkout_session_lost_stripe_response_recovers_with_same_idempotency_key(
    db_session: Session,
) -> None:
    """Случай «Stripe создал сессию, но ответ потерялся»: retry ползва същия idempotency_key."""
    stripe_created: dict[str, Any] = {}
    observed_keys: list[str] = []

    def _stripe_side_effect(**kwargs: Any) -> Any:
        key = kwargs["idempotency_key"]
        observed_keys.append(key)
        if key not in stripe_created:
            stripe_created[key] = SimpleNamespace(
                id="cs_lost_resp_77",
                url="https://checkout.stripe.com/pay/cs_lost_resp_77",
            )
            raise TimeoutError("Network drop after Stripe created session")
        return stripe_created[key]

    mock_client = MagicMock(spec=StripeClient)
    mock_client.create_checkout_session.side_effect = _stripe_side_effect
    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        req = build_internal_checkout_payload(
            order_id=77, attempt_key="order_77_attempt_1"
        )
        r1 = client.post(
            "/internal/payments/checkout-sessions",
            json=req,
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r1.status_code == 502
        assert r1.json()["detail"]["code"] == "STRIPE_ERROR"

        # В БД записът е 'initiated' със същия стабилен ключ
        attempt = db_session.query(PaymentAttempt).filter_by(order_id=77).one()
        assert attempt.status == "initiated"
        assert attempt.attempt_key == "order_77_attempt_1"

        # Повторен опит -> Stripe вижда същия idempotency_key и връща съществуващата cs_lost_resp_77
        r2 = client.post(
            "/internal/payments/checkout-sessions",
            json=req,
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r2.status_code == 200
        assert r2.json()["session_id"] == "cs_lost_resp_77"
        assert observed_keys == ["order_77_attempt_1", "order_77_attempt_1"]
        assert len(stripe_created) == 1

        db_session.refresh(attempt)
        assert attempt.status == "open"
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_new_attempt_after_failure_requires_fresh_key_and_rejects_old_terminal_key(
    db_session: Session,
) -> None:
    """Допустима нова попытка след окончателна неудача ползва нов attempt_key, а старият се отхвърля."""
    failed_attempt = PaymentAttempt(
        order_id=88,
        attempt_key="order_88_attempt_1",
        status="failed",
        amount_minor=4000,
        currency="EUR",
        mode="test",
    )
    db_session.add(failed_attempt)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=failed_attempt.id,
            order_id=88,
            status="failed",
            amount_minor=4000,
            currency="EUR",
            mode="test",
            stripe_session_id="cs_old_fail_88",
            checkout_url="https://checkout.stripe.com/pay/cs_old_fail_88",
            idempotency_key="order_88_attempt_1",
        )
    )
    db_session.commit()

    # Повтор със стария ключ -> отхвърля се (409 ATTEMPT_TERMINAL)
    r_stale = client.post(
        "/internal/payments/checkout-sessions",
        json=build_internal_checkout_payload(
            order_id=88, attempt_key="order_88_attempt_1", amount_minor=4000
        ),
        headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
    )
    assert r_stale.status_code == 409
    assert r_stale.json()["detail"]["code"] == "ATTEMPT_TERMINAL"

    # Нова попытка с нов ключ -> създава нова сесия с нов idempotency_key
    mock_client = MagicMock(spec=StripeClient)
    mock_client.create_checkout_session.return_value = SimpleNamespace(
        id="cs_new_retry_88",
        url="https://checkout.stripe.com/pay/cs_new_retry_88",
    )
    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        r_new = client.post(
            "/internal/payments/checkout-sessions",
            json=build_internal_checkout_payload(
                order_id=88, attempt_key="order_88_attempt_2", amount_minor=4000
            ),
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert r_new.status_code == 200
        assert r_new.json()["session_id"] == "cs_new_retry_88"
        assert (
            mock_client.create_checkout_session.call_args.kwargs["idempotency_key"]
            == "order_88_attempt_2"
        )
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_delayed_stripe_response_when_cancelled_expires_late_session(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Закъснял отговор от Stripe при вече отбелязан cancel прекратява късната сесия и не я отваря."""
    mock_client = MagicMock(spec=StripeClient)

    def _cancel_while_waiting(**kwargs: Any) -> Any:
        with session_factory() as s:
            att = s.query(PaymentAttempt).filter_by(order_id=91).one()
            sess = s.query(StripeCheckoutSession).filter_by(order_id=91).one()
            att.status = "cancelling"
            sess.status = "cancelling"
            s.commit()
        return SimpleNamespace(
            id="cs_late_91",
            url="https://checkout.stripe.com/pay/cs_late_91",
        )

    mock_client.create_checkout_session.side_effect = _cancel_while_waiting
    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        resp = client.post(
            "/internal/payments/checkout-sessions",
            json=build_internal_checkout_payload(order_id=91),
            headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
        )
        assert resp.status_code == 409
        mock_client.expire_checkout_session.assert_called_once_with("cs_late_91")

        att = db_session.query(PaymentAttempt).filter_by(order_id=91).one()
        assert att.status == "expired"
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_cancel_concurrent_expired_webhook_and_new_key_checkout_leaves_no_second_link(
    db_session: Session,
) -> None:
    """Сценарий 3: cancel започва -> идва expired за attempt_1 -> checkout с attempt_2 -> cancel завършва без втора платежна връзка."""
    attempt_1 = PaymentAttempt(
        order_id=96,
        attempt_key="order_96_attempt_1",
        status="open",
        amount_minor=4500,
        currency="EUR",
    )
    db_session.add(attempt_1)
    db_session.flush()
    db_session.add(
        StripeCheckoutSession(
            payment_attempt_id=attempt_1.id,
            order_id=96,
            status="open",
            amount_minor=4500,
            currency="EUR",
            stripe_session_id="cs_first_96",
            checkout_url="https://checkout.stripe.com/pay/cs_first_96",
            idempotency_key="order_96_attempt_1",
        )
    )
    db_session.commit()

    cancel_entered_stripe = threading.Event()
    allow_cancel_finish = threading.Event()

    def _blocking_expire(sid: str) -> Any:
        assert sid == "cs_first_96"
        cancel_entered_stripe.set()
        assert allow_cancel_finish.wait(timeout=5.0)
        return SimpleNamespace(id="cs_first_96", status="expired")

    mock_client = MagicMock(spec=StripeClient)
    mock_client.expire_checkout_session.side_effect = _blocking_expire
    mock_client.create_checkout_session.return_value = SimpleNamespace(
        id="cs_second_forbidden_96",
        url="https://checkout.stripe.com/pay/cs_second_forbidden_96",
    )

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            cancel_future = pool.submit(
                lambda: client.post(
                    "/internal/payments/orders/96/cancel",
                    json={"reason": "cancel"},
                    headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
                )
            )
            assert cancel_entered_stripe.wait(timeout=5.0)

            # 2. Идва webhook checkout.session.expired за първия опит (използваме реален construct_webhook_event)
            wh_body = json.dumps(
                {
                    "id": "evt_race_exp_96",
                    "type": "checkout.session.expired",
                    "livemode": False,
                    "data": {
                        "object": {
                            "id": "cs_first_96",
                            "livemode": False,
                            "status": "expired",
                            "client_reference_id": "96",
                            "metadata": {
                                "order_id": "96",
                                "attempt_key": "order_96_attempt_1",
                                "payment_attempt_id": str(attempt_1.id),
                            },
                        }
                    },
                }
            ).encode("utf-8")
            with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
                real_client = StripeClient(api_key="sk_test_unused")
                mock_client.construct_webhook_event.side_effect = (
                    real_client.construct_webhook_event
                )
                r_exp = client.post(
                    "/webhooks/stripe",
                    content=wh_body,
                    headers={
                        "Stripe-Signature": generate_stripe_signature(
                            wh_body, TEST_WEBHOOK_SECRET
                        )
                    },
                )
            assert r_exp.status_code == 200
            assert r_exp.json()["status"] == "expired"

            # 3. Междувременно идва checkout с нов ключ order_96_attempt_2 -> трябва да бъде отказан!
            r_retry_checkout = client.post(
                "/internal/payments/checkout-sessions",
                json=build_internal_checkout_payload(
                    order_id=96,
                    attempt_key="order_96_attempt_2",
                    amount_minor=4500,
                ),
                headers={"X-Internal-Token": TEST_INTERNAL_TOKEN},
            )

            # 4. Отмяната завършва
            allow_cancel_finish.set()
            r_cancel = cancel_future.result(timeout=5.0)
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)

    assert r_cancel.status_code == 200
    assert r_cancel.json()["status"] == "can_cancel"
    assert r_retry_checkout.status_code == 409
    assert r_retry_checkout.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
    assert mock_client.create_checkout_session.call_count == 0

    db_session.expire_all()
    sessions = db_session.query(StripeCheckoutSession).filter_by(order_id=96).all()
    attempts = db_session.query(PaymentAttempt).filter_by(order_id=96).all()
    assert len(attempts) == 1
    assert attempts[0].status == "expired"
    assert len(sessions) == 1
    assert sessions[0].status == "expired"
    assert not any(
        s.status in ("initiated", "open", "processing", "cancelling") for s in sessions
    )
