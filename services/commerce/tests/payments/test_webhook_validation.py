import json
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.orders.models import StripeCheckoutSession
from tests.payments.helpers import (
    TEST_WEBHOOK_SECRET,
    _create_order,
    _create_product,
    _create_user_and_token,
    _generate_stripe_signature,
)

client = TestClient(app)


def test_webhook_no_jwt_required(db_session: Session) -> None:
    """Уебхукът не изисква JWT автентикация."""
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post("/webhooks/stripe", content=b"")
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "INVALID_STRIPE_SIGNATURE"


def test_webhook_invalid_signature_returns_400(db_session: Session) -> None:
    """Подправен или грешен подпис връща 400 без промени в базата."""
    payload = json.dumps(
        {"id": "evt_123", "type": "checkout.session.completed"}
    ).encode()
    bad_sig = "t=1234567,v1=bad_hex_signature"

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": bad_sig},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "INVALID_STRIPE_SIGNATURE"


def test_webhook_stripe_not_configured_returns_503(db_session: Session) -> None:
    """Липса на STRIPE_WEBHOOK_SECRET връща 503."""
    payload = b'{"id": "evt_123"}'
    with patch.object(settings, "stripe_webhook_secret", None):
        resp = client.post(
            "/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": "t=1,v1=dummy"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "STRIPE_NOT_CONFIGURED"


def test_webhook_livemode_ignored_in_sandbox(db_session: Session) -> None:
    """Критерий: В sandbox режим уебхуци с livemode=True се игнорират и не маркират поръчката като платена."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=3000)
    order = _create_order(db_session, user.id, product, quantity=1)

    event_payload = {
        "id": "evt_live_123",
        "type": "checkout.session.completed",
        "livemode": True,
        "data": {
            "object": {
                "id": "cs_live_123",
                "payment_status": "paid",
                "amount_total": 3000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {"order_id": str(order.id), "attempt_id": "1"},
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
        assert resp.json()["status"] == "ignored"

    db_session.refresh(order)
    assert order.status == "pending_payment"


def test_webhook_mismatched_attempt_id(db_session: Session) -> None:
    """Критерий: Не е достатъчно да съвпадат само order_id и сумата; при несъвпадащ attempt_id поръчката не се маркира paid."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000)
    order = _create_order(db_session, user.id, product, quantity=1)

    # Подаваме несъществуващ attempt_id
    event_payload = {
        "id": "evt_bad_attempt_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_bad_attempt_1",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 2000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": "999999",
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
        assert resp.json()["status"] == "mismatched"

    db_session.refresh(order)
    assert order.status == "pending_payment"


def test_webhook_mismatched_amount_or_currency(db_session: Session) -> None:
    """Несъответствие в сума или валута не променя поръчката."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=3000)
    order = _create_order(db_session, user.id, product, quantity=1)  # 3000 EUR

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=3000,
        currency="EUR",
        stripe_session_id="cs_mismatch_123",
        checkout_url="https://checkout.stripe.com/pay/cs_mismatch_123",
        idempotency_key="idemp_mismatch_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    # 1. Грешна сума (1000 вместо 3000)
    event_bad_amount = {
        "id": "evt_bad_amt_789",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_mismatch_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 1000,
                "currency": "eur",
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    p_bytes = json.dumps(event_bad_amount).encode()
    sig = _generate_stripe_signature(p_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=p_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "mismatched"

    db_session.refresh(order)
    assert order.status == "pending_payment"

    # 2. Грешна валута (USD вместо EUR)
    event_bad_curr = {
        "id": "evt_bad_curr_789",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_mismatch_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 3000,
                "currency": "usd",
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    p_bytes_curr = json.dumps(event_bad_curr).encode()
    sig_curr = _generate_stripe_signature(p_bytes_curr, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp_c = client.post(
            "/webhooks/stripe",
            content=p_bytes_curr,
            headers={"Stripe-Signature": sig_curr},
        )
        assert resp_c.status_code == 200
        assert resp_c.json()["status"] == "mismatched"

    db_session.refresh(order)
    assert order.status == "pending_payment"


def test_webhook_non_existent_order_and_other_events(db_session: Session) -> None:
    """Несъществуваща поръчка и други събития се обработват безопасно."""
    evt_ghost = {
        "id": "evt_ghost_999",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_ghost_999",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "eur",
                "metadata": {"order_id": "888888", "attempt_id": "1"},
            }
        },
    }
    p_ghost = json.dumps(evt_ghost).encode()
    sig_ghost = _generate_stripe_signature(p_ghost, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=p_ghost,
            headers={"Stripe-Signature": sig_ghost},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "order_not_found"

    # Друг тип събитие (напр. charge.updated)
    evt_other = {
        "id": "evt_other_555",
        "type": "charge.updated",
        "livemode": False,
        "data": {"object": {"id": "ch_123"}},
    }
    p_other = json.dumps(evt_other).encode()
    sig_other = _generate_stripe_signature(p_other, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp_o = client.post(
            "/webhooks/stripe",
            content=p_other,
            headers={"Stripe-Signature": sig_other},
        )
        assert resp_o.status_code == 200
        assert resp_o.json()["status"] == "ignored"


def test_webhook_mode_mismatch_rejection(db_session: Session) -> None:
    """Събития от live в test режим или обратно се отхвърлят с reason='mode_mismatch'."""
    event_payload = {
        "id": "evt_livemode_mismatch_1",
        "type": "checkout.session.completed",
        "livemode": True,  # Livemode събитие при тест режим
        "data": {"object": {"id": "cs_live_123", "livemode": True}},
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with (
        patch.object(settings, "stripe_mode", "test"),
        patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET),
    ):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "mode_mismatch"
