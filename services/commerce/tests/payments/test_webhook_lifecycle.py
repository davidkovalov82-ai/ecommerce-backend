import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.orders.dependencies import get_stripe_client
from app.modules.orders.models import StripeCheckoutSession
from app.modules.orders.stripe_client import StripeClient
from tests.payments.helpers import (
    TEST_WEBHOOK_SECRET,
    _create_order,
    _create_product,
    _create_user_and_token,
    _generate_stripe_signature,
)

client = TestClient(app)


def test_webhook_checkout_session_completed_success_and_stock_preserved(
    db_session: Session,
) -> None:
    """Критерий: Успешен webhook маркира paid, без повторно сваляне на stock."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=3000, stock=8)
    order = _create_order(db_session, user.id, product, quantity=2)  # total: 6000
    initial_stock = product.stock  # 8 - 2 = 6

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=6000,
        currency="EUR",
        stripe_session_id="cs_completed_123",
        checkout_url="https://checkout.stripe.com/pay/cs_completed_123",
        idempotency_key="idemp_123",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_order_paid_123",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_completed_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 6000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    db_session.refresh(order)
    db_session.refresh(product)
    db_session.refresh(session_rec)

    assert order.status == "paid"
    # Складовата наличност НЕ е намалявана повторно!
    assert product.stock == initial_stock
    assert session_rec.status == "completed"

    event_exists = db_session.execute(
        text(
            "SELECT count(*) FROM stripe_processed_events "
            "WHERE stripe_event_id = 'evt_order_paid_123'"
        )
    ).scalar()
    assert event_exists == 1


def test_webhook_before_stripe_create_response_links_attempt(
    db_session: Session,
) -> None:
    """Критерий: Ако webhook пристигне преди завършване на create_checkout_session, сесията се свързва чрез записания опит."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=1)

    # Записваме опит в статус initiated БЕЗ stripe_session_id (Stripe create още не е върнал отговор в DB)
    attempt = StripeCheckoutSession(
        order_id=order.id,
        status="initiated",
        amount_minor=2000,
        currency="EUR",
        stripe_session_id=None,
        checkout_url=None,
        idempotency_key="idemp_early_1",
    )
    db_session.add(attempt)
    db_session.commit()

    # Webhook пристига преди create отговора!
    event_payload = {
        "id": "evt_early_arrival_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_early_stripe_id_999",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 2000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(attempt.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    db_session.refresh(order)
    db_session.refresh(attempt)
    assert order.status == "paid"
    # Сесията е коректно свързана с полученото Stripe ID и е маркирана като completed
    assert attempt.stripe_session_id == "cs_early_stripe_id_999"
    assert attempt.status == "completed"


def test_webhook_concurrent_deliveries_no_500_or_duplicate_mutation(
    db_session: Session,
) -> None:
    """Критерий: Две едновременни доставки на едно event ID не дават 500 и не прилагат промяната два пъти."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000, stock=10)
    order = _create_order(db_session, user.id, product, quantity=1)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2000,
        currency="EUR",
        stripe_session_id="cs_concurrent_123",
        checkout_url="https://checkout.stripe.com/pay/cs_concurrent_123",
        idempotency_key="idemp_conc_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_concurrent_delivery_123",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_concurrent_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 2000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    def _send_webhook() -> tuple[int, dict[str, Any]]:
        thread_client = TestClient(app)
        with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
            r = thread_client.post(
                "/webhooks/stripe",
                content=payload_bytes,
                headers={"Stripe-Signature": sig},
            )
            return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_send_webhook)
        f2 = executor.submit(_send_webhook)
        code1, data1 = f1.result()
        code2, data2 = f2.result()

    # И двете заявки връщат 200 OK (нито една не гърми с 500)
    assert code1 == 200
    assert code2 == 200
    statuses = [data1["status"], data2["status"]]
    assert "success" in statuses
    assert "already_processed" in statuses or statuses == ["success", "success"]

    # В базата има точно 1 запис за събитието
    count = db_session.execute(
        text(
            "SELECT count(*) FROM stripe_processed_events "
            "WHERE stripe_event_id = 'evt_concurrent_delivery_123'"
        )
    ).scalar()
    assert count == 1


def test_webhook_payment_unpaid_leaves_order_pending(db_session: Session) -> None:
    """Неуспешно плащане (напр. отхвърлена карта) оставя поръчката pending_payment."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000)
    order = _create_order(db_session, user.id, product, quantity=1)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2000,
        currency="EUR",
        stripe_session_id="cs_unpaid_456",
        checkout_url="https://checkout.stripe.com/pay/cs_unpaid_456",
        idempotency_key="idemp_unpaid_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_order_unpaid_456",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_unpaid_456",
                "livemode": False,
                "payment_status": "unpaid",
                "amount_total": 2000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "processing"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    assert order.status == "payment_processing"
    assert session_rec.status == "processing"


def test_webhook_async_payment_succeeded_marks_paid_stock_unchanged(
    db_session: Session,
) -> None:
    """Асинхронно потвърждение (async_payment_succeeded) превежда поръчката в paid, без промяна на stock."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "SEPA Product", stock=10, price_minor=5000)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock става 8
    order.status = "payment_processing"

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=10000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_async_succ_123",
        checkout_url="https://checkout.stripe.com/pay/cs_async_succ_123",
        idempotency_key="idemp_async_succ_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_async_succ_1",
        "type": "checkout.session.async_payment_succeeded",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_async_succ_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 10000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert order.status == "paid"
    assert session_rec.status == "completed"
    assert product.stock == 8  # Наличността не се намалява повторно!


def test_webhook_async_payment_failed_resets_pending_if_not_expired(
    db_session: Session,
) -> None:
    """Неуспешно отложено плащане преди изтичане на срока връща поръчката в pending_payment (клиентът може да опита пак)."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "SEPA Fail", stock=5, price_minor=4000)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.status = "payment_processing"
    order.expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        minutes=20
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=4000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_async_fail_123",
        checkout_url="https://checkout.stripe.com/pay/cs_async_fail_123",
        idempotency_key="idemp_async_fail_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_async_fail_1",
        "type": "checkout.session.async_payment_failed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_async_fail_123",
                "livemode": False,
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert session_rec.status == "failed"
    assert order.status == "pending_payment"
    assert product.stock == 4  # Резервацията се пази, защото срокът не е изтекъл

    # Клиентът може да създаде нов платежен линк
    mock_client = MagicMock(spec=StripeClient)
    mock_session = MagicMock()
    mock_session.id = "cs_retry_new_999"
    mock_session.url = "https://checkout.stripe.com/pay/cs_retry_new_999"
    mock_client.create_checkout_session.return_value = mock_session

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        resp2 = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["checkout_url"] == mock_session.url
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_webhook_async_payment_failed_expires_order_if_reserve_expired(
    db_session: Session,
) -> None:
    """Неуспешно отложено плащане след изтичане на срока маркира поръчката като expired и връща stock."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "SEPA Expired", stock=5, price_minor=4000)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock става 3
    order.status = "payment_processing"
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=5
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=8000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_async_exp_456",
        checkout_url="https://checkout.stripe.com/pay/cs_async_exp_456",
        idempotency_key="idemp_async_exp_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_async_exp_1",
        "type": "checkout.session.async_payment_failed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_async_exp_456",
                "livemode": False,
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert session_rec.status == "failed"
    assert order.status == "expired"
    assert product.stock == 5  # Наличността е възстановена точно веднъж!


def test_webhook_session_expired_event(db_session: Session) -> None:
    """Събитие checkout.session.expired маркира опита expired и освобождава стоката, ако срокът на поръчката е минал."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Timeout Prod", stock=7, price_minor=2000)
    order = _create_order(db_session, user.id, product, quantity=1)  # stock 6
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=1
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_sess_exp_789",
        checkout_url="https://checkout.stripe.com/pay/cs_sess_exp_789",
        idempotency_key="idemp_sess_exp_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_sess_exp_1",
        "type": "checkout.session.expired",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_sess_exp_789",
                "livemode": False,
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "expired"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert session_rec.status == "expired"
    assert order.status == "expired"
    assert product.stock == 7


def test_webhook_out_of_order_async_success_before_completed_unpaid(
    db_session: Session,
) -> None:
    """Ако async_payment_succeeded пристигне преди completed(unpaid), поръчката не се връща обратно в payment_processing."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Out of Order", stock=5, price_minor=3000)
    order = _create_order(db_session, user.id, product, quantity=1)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=3000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_ooo_1",
        checkout_url="https://checkout.stripe.com/pay/cs_ooo_1",
        idempotency_key="idemp_ooo_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    # 1. Пристига async_payment_succeeded първо
    evt_succ = {
        "id": "evt_ooo_succ",
        "type": "checkout.session.async_payment_succeeded",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_ooo_1",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 3000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    b_succ = json.dumps(evt_succ).encode()
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=b_succ,
            headers={
                "Stripe-Signature": _generate_stripe_signature(
                    b_succ, TEST_WEBHOOK_SECRET
                )
            },
        )
        assert resp.status_code == 200

    db_session.refresh(order)
    assert order.status == "paid"

    # 2. Закъсняло събитие completed с payment_status=unpaid
    evt_compl = {
        "id": "evt_ooo_compl",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_ooo_1",
                "livemode": False,
                "payment_status": "unpaid",
                "amount_total": 3000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    b_compl = json.dumps(evt_compl).encode()
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=b_compl,
            headers={
                "Stripe-Signature": _generate_stripe_signature(
                    b_compl, TEST_WEBHOOK_SECRET
                )
            },
        )
        assert resp.status_code == 200

    db_session.refresh(order)
    assert order.status == "paid"  # Не е върната в payment_processing!


def test_webhook_discrepancy_payment_on_cancelled_or_expired_order(
    db_session: Session,
) -> None:
    """Плащане върху отказана поръчка връща status='discrepancy' и НЕ променя наличностите тихомълком."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Discrepancy Prod", stock=5, price_minor=2500)
    order = _create_order(db_session, user.id, product, quantity=1)  # stock 4
    # Потребителят е отказал поръчката: наличността е възстановена на 5
    order.status = "cancelled"
    product.stock = 5

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2500,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_discrep_1",
        checkout_url="https://checkout.stripe.com/pay/cs_discrep_1",
        idempotency_key="idemp_discrep_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_discrep_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_discrep_1",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 2500,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "discrepancy"
        assert resp.json()["reason"] == "payment_on_cancelled_order"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert order.status == "cancelled"
    assert session_rec.status == "completed"
    assert product.stock == 5  # Не е пипнат!
