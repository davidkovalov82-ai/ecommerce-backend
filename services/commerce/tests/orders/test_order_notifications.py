import datetime
import json
import time
import urllib.request
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.main import app
from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.mailer import EmailSender
from app.modules.orders.models import (
    Order,
    OrderOutboxEvent,
    StripeCheckoutSession,
)
from app.modules.orders.outbox_processor import OutboxProcessor
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from app.modules.orders.stripe_client import StripeClient
from tests.orders.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
)
from tests.payments.helpers import (
    TEST_WEBHOOK_SECRET,
    _generate_stripe_signature,
)

client = TestClient(app)


def test_webhook_completed_creates_outbox_event(db_session: Session) -> None:
    """Критерий: Незабавен webhook (completed + paid) записва събитие order.paid в outbox."""
    user, _ = _create_user_and_token(db_session, email="buyer_webhook@example.com")
    product = _create_product(db_session, "Outbox Item", price_minor=4500, stock=5)
    order = _create_order(db_session, user.id, product, quantity=1)  # 4500 EUR

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=4500,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_outbox_paid_1",
        checkout_url="https://checkout.stripe.com/pay/cs_outbox_paid_1",
        idempotency_key="idemp_outbox_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_outbox_paid_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_outbox_paid_1",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 4500,
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
    assert order.status == "paid"

    # Проверка на outbox записа в същата база
    repo = OrderRepository(db_session)
    outbox_ev = repo.get_outbox_event(order.id, "order.paid")
    assert outbox_ev is not None
    assert outbox_ev.status == "pending"
    assert outbox_ev.retry_count == 0
    assert outbox_ev.payload["order_id"] == order.id
    assert outbox_ev.payload["customer_email"] == "buyer_webhook@example.com"
    assert outbox_ev.payload["total_minor"] == 4500
    assert outbox_ev.payload["currency"] == "EUR"


def test_webhook_async_payment_succeeded_creates_outbox_event(
    db_session: Session,
) -> None:
    """Критерий: Отложено плащане (async_payment_succeeded) записва събитие order.paid в outbox."""
    user, _ = _create_user_and_token(db_session, email="buyer_async@example.com")
    product = _create_product(db_session, "Async Item", price_minor=3000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.status = "payment_processing"

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=3000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_outbox_async_1",
        checkout_url="https://checkout.stripe.com/pay/cs_outbox_async_1",
        idempotency_key="idemp_outbox_async_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_outbox_async_1",
        "type": "checkout.session.async_payment_succeeded",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_outbox_async_1",
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
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200

    db_session.refresh(order)
    assert order.status == "paid"

    repo = OrderRepository(db_session)
    outbox_ev = repo.get_outbox_event(order.id, "order.paid")
    assert outbox_ev is not None
    assert outbox_ev.status == "pending"
    assert outbox_ev.payload["customer_email"] == "buyer_async@example.com"
    assert outbox_ev.payload["total_minor"] == 3000


def test_reconciliation_creates_outbox_event(db_session: Session) -> None:
    """Критерий: При преход в paid през периодична сверка се записва събитие order.paid в outbox."""
    user, _ = _create_user_and_token(db_session, email="buyer_rec@example.com")
    product = _create_product(db_session, "Rec Item", price_minor=2000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=10
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_paid_outbox",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_paid_outbox",
        idempotency_key="idemp_rec_outbox",
    )
    db_session.add(session_rec)
    db_session.commit()

    mock_stripe = MagicMock(spec=StripeClient)
    m = MagicMock()
    m.status = "complete"
    m.payment_status = "paid"
    mock_stripe.retrieve_checkout_session.return_value = m

    service = OrderService(
        order_repository=OrderRepository(db_session),
        cart_repository=CartRepository(db_session),
        product_repository=ProductRepository(db_session),
        user_repository=UserRepository(db_session),
        session=db_session,
        stripe_client=mock_stripe,
    )
    stats = service.reconcile_unresolved_orders()
    assert stats["paid"] == 1

    db_session.refresh(order)
    assert order.status == "paid"

    repo = OrderRepository(db_session)
    outbox_ev = repo.get_outbox_event(order.id, "order.paid")
    assert outbox_ev is not None
    assert outbox_ev.status == "pending"
    assert outbox_ev.payload["order_id"] == order.id


def test_duplicate_webhook_does_not_create_duplicate_outbox_event(
    db_session: Session,
) -> None:
    """Критерий: Дублиран webhook не създава второ събитие в outbox таблицата."""
    user, _ = _create_user_and_token(db_session, email="dup@example.com")
    product = _create_product(db_session, "Dup Item", price_minor=1000)
    order = _create_order(db_session, user.id, product, quantity=1)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=1000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_dup_outbox",
        checkout_url="https://checkout.stripe.com/pay/cs_dup_outbox",
        idempotency_key="idemp_dup_outbox",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_dup_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_dup_outbox",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 1000,
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
        # 1. Първа доставка
        r1 = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert r1.status_code == 200

        # 2. Повторна доставка
        r2 = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert r2.status_code == 200

    # Проверка, че съществува ТОЧНО ЕДНО събитие в order_outbox_events
    events = (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.order_id == order.id)
        .all()
    )
    assert len(events) == 1


def test_duplicate_reconciliation_does_not_create_duplicate_outbox_event(
    db_session: Session,
) -> None:
    """Критерий: Повторна сверка не създава второ събитие в outbox таблицата."""
    user, _ = _create_user_and_token(db_session, email="dup_rec@example.com")
    product = _create_product(db_session, "Dup Rec Item", price_minor=1000)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=5
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="completed",
        amount_minor=1000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_dup_rec_outbox",
        checkout_url="https://checkout.stripe.com/pay/cs_dup_rec_outbox",
        idempotency_key="idemp_dup_rec_outbox",
    )
    db_session.add(session_rec)
    db_session.commit()

    service = OrderService(
        order_repository=OrderRepository(db_session),
        cart_repository=CartRepository(db_session),
        product_repository=ProductRepository(db_session),
        user_repository=UserRepository(db_session),
        session=db_session,
        stripe_client=MagicMock(spec=StripeClient),
    )

    # Първа сверка
    service.reconcile_unresolved_orders()
    # Втора сверка
    service.reconcile_unresolved_orders()

    events = (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.order_id == order.id)
        .all()
    )
    assert len(events) == 1


def test_rollback_before_commit_does_not_leave_outbox_event(
    db_session: Session,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Критерий: При rollback не остава събитие за непотвърдено плащане."""
    user, _ = _create_user_and_token(db_session, email="rollback@example.com")
    product = _create_product(db_session, "Rollback Item", price_minor=1500)
    order = _create_order(db_session, user.id, product, quantity=1)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=1500,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rb_outbox",
        checkout_url="https://checkout.stripe.com/pay/cs_rb_outbox",
        idempotency_key="idemp_rb_outbox",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_rb_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_rb_outbox",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 1500,
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

    original_commit = Session.commit

    def _failing_commit(self: Session) -> None:
        raise RuntimeError("Simulated DB failure right before commit")

    monkeypatch.setattr(Session, "commit", _failing_commit)

    with (
        patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET),
        pytest.raises(RuntimeError, match="Simulated DB failure"),
    ):
        client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )

    monkeypatch.setattr(Session, "commit", original_commit)

    # Проверка през независима чиста сесия: НИТО поръчката е paid, НИТО има outbox събитие!
    with session_factory() as verification_session:
        db_order = verification_session.get(Order, order.id)
        assert db_order is not None
        assert db_order.status == "pending_payment"

        outbox_count = (
            verification_session.query(OrderOutboxEvent)
            .filter(OrderOutboxEvent.order_id == order.id)
            .count()
        )
        assert outbox_count == 0


def test_no_outbox_event_for_pending_cancelled_expired(db_session: Session) -> None:
    """Критерий: За поръчки в pending_payment, cancelled или expired не се създава събитие в outbox."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Status Test", price_minor=1000)

    # 1. Поръчка в pending_payment
    order1 = _create_order(db_session, user.id, product)
    events1 = (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.order_id == order1.id)
        .all()
    )
    assert len(events1) == 0

    # 2. Отказ от поръчка -> статус cancelled
    client.post(
        f"/orders/{order1.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    db_session.refresh(order1)
    assert order1.status == "cancelled"
    events_cancelled = (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.order_id == order1.id)
        .all()
    )
    assert len(events_cancelled) == 0

    # 3. Изтекъл резерв -> статус expired
    order2 = _create_order(db_session, user.id, product)
    order2.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=10
    )
    db_session.commit()

    service = OrderService(
        order_repository=OrderRepository(db_session),
        cart_repository=CartRepository(db_session),
        product_repository=ProductRepository(db_session),
        user_repository=UserRepository(db_session),
        session=db_session,
        stripe_client=MagicMock(spec=StripeClient),
    )
    service.reconcile_unresolved_orders()
    db_session.refresh(order2)
    assert order2.status == "expired"
    events_expired = (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.order_id == order2.id)
        .all()
    )
    assert len(events_expired) == 0


def test_outbox_processor_sends_email_and_marks_sent(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: OutboxProcessor изпраща имейл с данни за поръчката и маркира записа като 'sent'."""
    user, _ = _create_user_and_token(db_session, email="customer_mailer@example.com")
    product = _create_product(db_session, "Mailer Prod", price_minor=7999)
    order = _create_order(db_session, user.id, product, quantity=1)

    outbox_event = OrderOutboxEvent(
        order_id=order.id,
        event_type="order.paid",
        payload={
            "order_id": order.id,
            "customer_email": "customer_mailer@example.com",
            "total_minor": 7999,
            "currency": "EUR",
        },
        status="pending",
    )
    db_session.add(outbox_event)
    db_session.commit()

    mock_mailer = MagicMock(spec=EmailSender)

    processor = OutboxProcessor(
        session_factory=session_factory,
        email_sender=mock_mailer,
    )
    stats = processor.process_pending_events()

    assert stats["processed"] == 1
    assert stats["sent"] == 1
    assert stats["failed"] == 0

    # Проверка, че send_order_paid_email е извикан с коректните аргументи
    mock_mailer.send_order_paid_email.assert_called_once_with(
        to_email="customer_mailer@example.com",
        order_id=order.id,
        total_minor=7999,
        currency="EUR",
    )

    # Проверка в базата: събитието е маркирано като sent
    with session_factory() as s:
        db_event = s.get(OrderOutboxEvent, outbox_event.id)
        assert db_event is not None
        assert db_event.status == "sent"
        assert db_event.processed_at is not None
        assert db_event.retry_count == 0


def test_outbox_processor_smtp_failure_keeps_order_paid_and_event_pending(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: При недостъпен SMTP поръчката остава paid, а събитието остава pending с retry_count = 1."""
    user, _ = _create_user_and_token(db_session, email="fail_smtp@example.com")
    product = _create_product(db_session, "Fail SMTP", price_minor=5000)
    order = _create_order(db_session, user.id, product, status="paid")

    outbox_event = OrderOutboxEvent(
        order_id=order.id,
        event_type="order.paid",
        payload={
            "order_id": order.id,
            "customer_email": "fail_smtp@example.com",
            "total_minor": 5000,
            "currency": "EUR",
        },
        status="pending",
    )
    db_session.add(outbox_event)
    db_session.commit()

    mock_mailer = MagicMock(spec=EmailSender)
    mock_mailer.send_order_paid_email.side_effect = ConnectionRefusedError(
        "Mailpit SMTP connection refused on port 1025"
    )

    processor = OutboxProcessor(
        session_factory=session_factory,
        email_sender=mock_mailer,
    )
    stats = processor.process_pending_events()

    assert stats["processed"] == 1
    assert stats["sent"] == 0
    assert stats["failed"] == 1

    # Проверка в базата:
    with session_factory() as s:
        db_order = s.get(Order, order.id)
        assert db_order is not None
        assert db_order.status == "paid"  # Поръчката остава paid!

        db_event = s.get(OrderOutboxEvent, outbox_event.id)
        assert db_event is not None
        assert db_event.status == "pending"  # Остава pending за повторен опит!
        assert db_event.retry_count == 1
        assert "Mailpit SMTP connection refused" in (db_event.last_error or "")


def test_outbox_processor_retry_after_smtp_recovery_succeeds(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: След неуспешен опит при възстановен SMTP следващият опит успява и маркира събитието 'sent'."""
    user, _ = _create_user_and_token(db_session, email="recover@example.com")
    product = _create_product(db_session, "Recover SMTP", price_minor=3000)
    order = _create_order(db_session, user.id, product, status="paid")

    outbox_event = OrderOutboxEvent(
        order_id=order.id,
        event_type="order.paid",
        payload={
            "order_id": order.id,
            "customer_email": "recover@example.com",
            "total_minor": 3000,
            "currency": "EUR",
        },
        status="pending",
        retry_count=1,
        last_error="Previous network timeout",
    )
    db_session.add(outbox_event)
    db_session.commit()

    mock_mailer = MagicMock(spec=EmailSender)  # Успешен SMTP

    processor = OutboxProcessor(
        session_factory=session_factory,
        email_sender=mock_mailer,
    )
    stats = processor.process_pending_events()

    assert stats["processed"] == 1
    assert stats["sent"] == 1
    assert stats["failed"] == 0

    with session_factory() as s:
        db_event = s.get(OrderOutboxEvent, outbox_event.id)
        assert db_event is not None
        assert db_event.status == "sent"
        assert db_event.processed_at is not None
        assert db_event.retry_count == 1  # Пази историята от предишния опит


def test_outbox_processor_idempotent_after_sent(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: Повторен запуск на командата след успех не изпраща ново писмо."""
    user, _ = _create_user_and_token(db_session, email="idemp@example.com")
    product = _create_product(db_session, "Idemp SMTP", price_minor=2000)
    order = _create_order(db_session, user.id, product, status="paid")

    outbox_event = OrderOutboxEvent(
        order_id=order.id,
        event_type="order.paid",
        payload={
            "order_id": order.id,
            "customer_email": "idemp@example.com",
            "total_minor": 2000,
            "currency": "EUR",
        },
        status="sent",
        processed_at=datetime.datetime.now(datetime.UTC),
    )
    db_session.add(outbox_event)
    db_session.commit()

    mock_mailer = MagicMock(spec=EmailSender)

    processor = OutboxProcessor(
        session_factory=session_factory,
        email_sender=mock_mailer,
    )
    stats = processor.process_pending_events()

    assert stats["processed"] == 0
    assert stats["sent"] == 0
    assert stats["failed"] == 0
    assert mock_mailer.send_order_paid_email.call_count == 0


def test_outbox_database_constraints(db_session: Session) -> None:
    """Критерий: UNIQUE(order_id, event_type) и CHECK(status) ограничения на outbox таблицата."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product)

    ev1 = OrderOutboxEvent(
        order_id=order.id,
        event_type="order.paid",
        payload={"order_id": order.id},
        status="pending",
    )
    db_session.add(ev1)
    db_session.commit()

    # 1. Опит за второ събитие 'order.paid' за същата поръчка -> UNIQUE ограничение
    ev2 = OrderOutboxEvent(
        order_id=order.id,
        event_type="order.paid",
        payload={"order_id": order.id},
        status="pending",
    )
    db_session.add(ev2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 2. Невалиден статус извън ('pending', 'sent', 'failed') -> CHECK ограничение
    ev_invalid_status = OrderOutboxEvent(
        order_id=order.id,
        event_type="order.shipped",
        payload={"order_id": order.id},
        status="unknown_status",
    )
    db_session.add(ev_invalid_status)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def _is_mailpit_live() -> bool:
    """Проверява дали Mailpit Web API е достъпен на localhost:8025."""
    try:
        req = urllib.request.Request("http://127.0.0.1:8025/api/v1/messages")
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.status == 200
    except (OSError, TimeoutError):
        return False


def _get_messages_for_recipient(recipient_email: str) -> list[dict[str, Any]]:
    """Извлича съобщенията от Mailpit API само за конкретен получател."""
    req = urllib.request.Request("http://127.0.0.1:8025/api/v1/messages")
    with urllib.request.urlopen(req, timeout=2) as r:
        data = json.loads(r.read().decode())
    return [
        m
        for m in data.get("messages", [])
        if any(to.get("Address") == recipient_email for to in m.get("To", []))
    ]


def _wait_for_messages_for_recipient(
    recipient_email: str,
    expected_count: int,
    timeout_seconds: float = 5.0,
    poll_interval: float = 0.1,
) -> list[dict[str, Any]]:
    """Изчаква с краен срок (deadline) получените писма за даден получател."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        msgs = _get_messages_for_recipient(recipient_email)
        if len(msgs) == expected_count:
            return msgs
        time.sleep(poll_interval)
    return _get_messages_for_recipient(recipient_email)


def _get_message_detail(message_id: str) -> dict[str, Any]:
    """Извлича детайлно съдържание на писмо (заглавие, текст) от Mailpit по ID."""
    req = urllib.request.Request(f"http://127.0.0.1:8025/api/v1/message/{message_id}")
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read().decode())


def test_notifications_retry_e2e_live_mailpit(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """E2E интеграционен тест с реален Mailpit и retry цикъл при SMTP срив и възстановяване.

    Сценарий:
    1. Един подписан Stripe webhook превежда поръчката в 'paid' и записва outbox събитие.
    2. Отказана SMTP връзка: поръчката остава 'paid', събитието 'pending' с грешка, 0 писма за получателя.
    3. Успешен повторен опит: писмото е доставено (проверка на номер на поръчка, сума и валута).
    4. Точно едно писмо за този получател и след третото пускане на обработката.
    """
    # 1. Липсата на Mailpit проваля теста (в CI и локално), без skip
    assert _is_mailpit_live(), (
        "Mailpit услугата не е достъпна на http://127.0.0.1:8025 - проверката се проваля. "
        "Уверете се, че Mailpit работи (напр. през 'make test-notifications-retry' или като service в CI)."
    )

    # 2. Уникален получател за теста (без глобално изтриване на пощенската кутия)
    customer_email = f"order_retry_{uuid.uuid4().hex[:12]}@example.com"
    assert len(_get_messages_for_recipient(customer_email)) == 0, (
        f"Кутията вече съдържа писма за новия уникален получател {customer_email}"
    )

    # Стъпка 1: Един подписан Stripe webhook
    user, _ = _create_user_and_token(db_session, email=customer_email)
    product = _create_product(db_session, "Retry Product", price_minor=4500)
    order = _create_order(
        db_session, user.id, product, quantity=1
    )  # 4500 EUR (45.00 EUR)

    event_id = f"evt_retry_{uuid.uuid4().hex[:12]}"
    session_id = f"cs_retry_{uuid.uuid4().hex[:12]}"
    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=4500,
        currency="EUR",
        mode="test",
        stripe_session_id=session_id,
        checkout_url="https://checkout.stripe.com/pay/cs_retry_test",
        idempotency_key=f"idemp_{session_id}",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": event_id,
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": session_id,
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 4500,
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
    assert resp.status_code == 200, (
        f"Стъпка 1 пропадна: Webhook върна статус {resp.status_code}"
    )

    with session_factory() as s:
        db_order = s.get(Order, order.id)
        assert db_order is not None and db_order.status == "paid", (
            "Стъпка 1 пропадна: Поръчката не е в статус 'paid'"
        )
        event = OrderRepository(s).get_outbox_event(order.id, "order.paid")
        assert event is not None, "Стъпка 1 пропадна: Липсва outbox събитие order.paid"
        assert event.status == "pending", (
            f"Стъпка 1 пропадна: Очаква се статус pending, получен {event.status}"
        )
        assert event.retry_count == 0
        outbox_id = event.id

    # Стъпка 2: Отказана SMTP връзка (недостъпен порт 19999)
    failing_mailer = EmailSender(host="127.0.0.1", port=19999, timeout=2.0)
    failing_processor = OutboxProcessor(
        session_factory=session_factory,
        email_sender=failing_mailer,
    )
    stats_fail = failing_processor.process_pending_events()
    assert stats_fail["processed"] >= 1, (
        "Стъпка 2 пропадна: Процесорът не обработи чакащото събитие"
    )
    assert stats_fail["failed"] >= 1, (
        "Стъпка 2 пропадна: Очаква се неуспех при недостъпен SMTP"
    )
    assert stats_fail["sent"] == 0, (
        "Стъпка 2 пропадна: Не бива да се изпращат писма при срив"
    )

    # Поръчката остава paid, събитието остава pending с увеличен retry_count
    with session_factory() as s:
        db_order = s.get(Order, order.id)
        assert db_order is not None and db_order.status == "paid", (
            "Стъпка 2 пропадна: Поръчката не остана в статус 'paid' след SMTP срив"
        )
        db_event = s.get(OrderOutboxEvent, outbox_id)
        assert db_event is not None
        assert db_event.status == "pending", (
            "Стъпка 2 пропадна: Събитието не остана в статус 'pending' за повторен опит"
        )
        assert db_event.retry_count == 1, (
            f"Стъпка 2 пропадна: Очаква се retry_count=1, получен {db_event.retry_count}"
        )
        assert db_event.last_error is not None, (
            "Стъпка 2 пропадна: Липсва записана грешка в last_error"
        )

    # Проверка, че няма изпратени писма за този получател
    assert len(_get_messages_for_recipient(customer_email)) == 0, (
        "Стъпка 2 пропадна: Mailpit съдържа писма за получателя след неуспешен опит"
    )

    # Стъпка 3: Възстановяване на SMTP (реален Mailpit порт 1025) и успешен повторен опит
    live_mailer = EmailSender(host="127.0.0.1", port=1025)
    live_processor = OutboxProcessor(
        session_factory=session_factory,
        email_sender=live_mailer,
    )
    stats_retry = live_processor.process_pending_events()
    assert stats_retry["sent"] >= 1, (
        "Стъпка 3 пропадна: Повторният опит не отчете изпратено писмо"
    )
    assert stats_retry["failed"] == 0, (
        "Стъпка 3 пропадна: Повторният опит отчете неочаквана грешка"
    )

    with session_factory() as s:
        db_event = s.get(OrderOutboxEvent, outbox_id)
        assert db_event is not None
        assert db_event.status == "sent", (
            f"Стъпка 3 пропадна: Събитието не е 'sent', текущ статус: {db_event.status}"
        )
        assert db_event.processed_at is not None, (
            "Стъпка 3 пропадна: Липсва processed_at timestamp"
        )

    # Изчакваме доставката с краен срок (краен срок 5.0s)
    delivered_msgs = _wait_for_messages_for_recipient(
        customer_email, expected_count=1, timeout_seconds=5.0
    )
    assert len(delivered_msgs) == 1, (
        f"Стъпка 3 пропадна: Очаква се точно 1 писмо за {customer_email}, намерени {len(delivered_msgs)}"
    )

    # Проверяваме номер на поръчката, сума и валута
    msg_summary = delivered_msgs[0]
    assert f"#{order.id}" in msg_summary["Subject"], (
        f"Стъпка 3 пропадна: Липсва номер на поръчка #{order.id} в заглавието: {msg_summary['Subject']}"
    )
    assert any(
        to.get("Address") == customer_email for to in msg_summary.get("To", [])
    ), (
        f"Стъпка 3 пропадна: Получателят {customer_email} не съвпада: {msg_summary.get('To')}"
    )

    detail = _get_message_detail(msg_summary["ID"])
    text_body = detail.get("Text", "")
    assert f"#{order.id}" in text_body, (
        f"Стъпка 3 пропадна: Липсва номер на поръчка #{order.id} в тялото: {text_body}"
    )
    assert "45.00" in text_body, (
        f"Стъпка 3 пропадна: Липсва форматирана сума 45.00 в тялото: {text_body}"
    )
    assert "EUR" in text_body, (
        f"Стъпка 3 пропадна: Липсва валута EUR в тялото: {text_body}"
    )

    # Стъпка 4: Трето пускане на обработката – точно едно писмо за този получател
    stats_3rd = live_processor.process_pending_events()
    assert stats_3rd["sent"] == 0, (
        "Стъпка 4 пропадна: Изпратено е дублирано писмо при 3-тото пускане"
    )
    assert stats_3rd["processed"] == 0, (
        "Стъпка 4 пропадна: Обработено е събитие при 3-тото пускане"
    )

    # Проверяваме, че писмата за този получател остават точно 1 и след третото пускане
    final_msgs = _get_messages_for_recipient(customer_email)
    assert len(final_msgs) == 1, (
        f"Стъпка 4 пропадна: След третото пускане писмата за {customer_email} не са точно 1 ({len(final_msgs)})"
    )
