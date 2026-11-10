"""Тестове на /internal/payments/checkout-sessions: защита, конкурентност, загуба на отговор и нови опити."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.dependencies import get_stripe_client
from app.main import app
from app.models import PaymentAttempt, StripeCheckoutSession
from app.stripe_client import StripeClient
from tests.helpers import TEST_INTERNAL_TOKEN, build_internal_checkout_payload

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
