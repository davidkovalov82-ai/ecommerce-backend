import datetime
import logging
import uuid
from unittest.mock import MagicMock

import pika.exceptions
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.dependencies import get_payments_client
from app.modules.orders.models import (
    Order,
    OrderOutboxEvent,
)
from app.modules.orders.outbox_processor import OutboxProcessor
from app.modules.orders.payments_client import (
    CancelPaymentResult,
    PaymentsClient,
    RemoteOrderAlreadyPaid,
)
from app.modules.orders.publisher import (
    BrokerUnavailableError,
    EventPublisher,
    PublishConfirmLostError,
    RabbitMQEventPublisher,
    UnroutableEventError,
)
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from tests.orders.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
)
from tests.payments.helpers import make_payment_event

client = TestClient(app)


def test_payment_succeeded_event_creates_outbox_event(db_session: Session) -> None:
    """Критерий: payment.succeeded от Payments записва събитие order.paid в outbox."""
    user, _ = _create_user_and_token(db_session, email="buyer_webhook@example.com")
    product = _create_product(db_session, "Outbox Item", price_minor=4500, stock=5)
    order = _create_order(db_session, user.id, product, quantity=1)  # 4500 EUR

    svc = OrderService(
        order_repository=OrderRepository(db_session),
        cart_repository=CartRepository(db_session),
        product_repository=ProductRepository(db_session),
        user_repository=UserRepository(db_session),
        session=db_session,
        payments_client=MagicMock(spec=PaymentsClient),
    )
    outcome = svc.apply_payment_event(
        make_payment_event(
            event_type="payment.succeeded",
            order_id=order.id,
            amount_minor=4500,
        )
    )
    assert outcome == "paid"

    db_session.refresh(order)
    assert order.status == "paid"

    repo = OrderRepository(db_session)
    outbox_ev = repo.get_outbox_event(order.id, "order.paid")
    assert outbox_ev is not None
    assert outbox_ev.status == "pending"
    assert outbox_ev.retry_count == 0
    assert uuid.UUID(outbox_ev.event_id)
    assert outbox_ev.event_version == 1
    assert outbox_ev.occurred_at is not None
    assert outbox_ev.payload["order_id"] == order.id
    assert outbox_ev.payload["customer_email"] == "buyer_webhook@example.com"
    assert outbox_ev.payload["total_minor"] == 4500
    assert outbox_ev.payload["currency"] == "EUR"


def test_delayed_payment_succeeded_after_processing_creates_outbox_event(
    db_session: Session,
) -> None:
    """Критерий: Отложено payment.succeeded след payment.processing записва order.paid в outbox."""
    user, _ = _create_user_and_token(db_session, email="buyer_async@example.com")
    product = _create_product(db_session, "Async Item", price_minor=3000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.status = "payment_processing"
    db_session.commit()

    svc = OrderService(
        order_repository=OrderRepository(db_session),
        cart_repository=CartRepository(db_session),
        product_repository=ProductRepository(db_session),
        user_repository=UserRepository(db_session),
        session=db_session,
        payments_client=MagicMock(spec=PaymentsClient),
    )
    outcome = svc.apply_payment_event(
        make_payment_event(
            event_type="payment.succeeded",
            order_id=order.id,
            amount_minor=3000,
        )
    )
    assert outcome == "paid"

    db_session.refresh(order)
    assert order.status == "paid"

    repo = OrderRepository(db_session)
    outbox_ev = repo.get_outbox_event(order.id, "order.paid")
    assert outbox_ev is not None
    assert outbox_ev.status == "pending"
    assert outbox_ev.payload["customer_email"] == "buyer_async@example.com"
    assert outbox_ev.payload["total_minor"] == 3000


def test_reconciliation_defers_when_payments_reports_paid_until_event_arrives(
    db_session: Session,
) -> None:
    """Критерий: Когато Payments докладва already_paid при просрочка, Commerce пази резерва за payment.succeeded."""
    user, _ = _create_user_and_token(db_session, email="buyer_rec@example.com")
    product = _create_product(db_session, "Rec Item", price_minor=2000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=10
    )
    db_session.commit()

    mock_payments = MagicMock(spec=PaymentsClient)
    mock_payments.cancel_order_payment.side_effect = RemoteOrderAlreadyPaid(
        "Paid in Payments"
    )

    service = OrderService(
        order_repository=OrderRepository(db_session),
        cart_repository=CartRepository(db_session),
        product_repository=ProductRepository(db_session),
        user_repository=UserRepository(db_session),
        session=db_session,
        payments_client=mock_payments,
    )
    stats = service.reconcile_expired_orders()
    assert stats["deferred"] == 1
    assert stats["expired"] == 0

    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == "pending_payment"
    assert product.stock == 4

    outcome = service.apply_payment_event(
        make_payment_event(
            event_type="payment.succeeded",
            order_id=order.id,
            amount_minor=2000,
        )
    )
    assert outcome == "paid"
    repo = OrderRepository(db_session)
    outbox_ev = repo.get_outbox_event(order.id, "order.paid")
    assert outbox_ev is not None
    assert outbox_ev.status == "pending"
    assert outbox_ev.payload["order_id"] == order.id


def test_duplicate_payment_event_does_not_create_duplicate_outbox_event(
    db_session: Session,
) -> None:
    """Критерий: Дублирано платежно събитие не създава второ събитие в outbox таблицата."""
    user, _ = _create_user_and_token(db_session, email="dup@example.com")
    product = _create_product(db_session, "Dup Item", price_minor=1000)
    order = _create_order(db_session, user.id, product, quantity=1)

    svc = OrderService(
        order_repository=OrderRepository(db_session),
        cart_repository=CartRepository(db_session),
        product_repository=ProductRepository(db_session),
        user_repository=UserRepository(db_session),
        session=db_session,
        payments_client=MagicMock(spec=PaymentsClient),
    )
    ev = make_payment_event(
        event_type="payment.succeeded",
        order_id=order.id,
        amount_minor=1000,
        event_id=str(uuid.uuid4()),
    )
    assert svc.apply_payment_event(ev) == "paid"
    assert svc.apply_payment_event(ev) == "already_processed"

    events = (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.order_id == order.id)
        .all()
    )
    assert len(events) == 1


def test_duplicate_reconciliation_does_not_create_duplicate_outbox_event(
    db_session: Session,
) -> None:
    """Критерий: Повторна сверка за просрочена поръчка не създава излишен outbox запис."""
    user, _ = _create_user_and_token(db_session, email="dup_rec@example.com")
    product = _create_product(db_session, "Dup Rec Item", price_minor=1000)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=5
    )
    db_session.commit()

    mock_payments = MagicMock(spec=PaymentsClient)
    mock_payments.cancel_order_payment.return_value = CancelPaymentResult(
        status="can_cancel", action="no_session"
    )
    service = OrderService(
        order_repository=OrderRepository(db_session),
        cart_repository=CartRepository(db_session),
        product_repository=ProductRepository(db_session),
        user_repository=UserRepository(db_session),
        session=db_session,
        payments_client=mock_payments,
    )

    service.reconcile_expired_orders()
    service.reconcile_expired_orders()

    events = (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.order_id == order.id)
        .all()
    )
    assert len(events) == 0


def test_rollback_before_commit_does_not_leave_outbox_event(
    db_session: Session,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Критерий: При rollback не остава събитие за непотвърдено плащане."""
    user, _ = _create_user_and_token(db_session, email="rollback@example.com")
    product = _create_product(db_session, "Rollback Item", price_minor=1500)
    order = _create_order(db_session, user.id, product, quantity=1)

    svc = OrderService(
        order_repository=OrderRepository(db_session),
        cart_repository=CartRepository(db_session),
        product_repository=ProductRepository(db_session),
        user_repository=UserRepository(db_session),
        session=db_session,
        payments_client=MagicMock(spec=PaymentsClient),
    )
    ev = make_payment_event(
        event_type="payment.succeeded",
        order_id=order.id,
        amount_minor=1500,
    )

    original_commit = Session.commit

    def _failing_commit(self: Session) -> None:
        raise RuntimeError("Simulated DB failure right before commit")

    monkeypatch.setattr(Session, "commit", _failing_commit)

    with pytest.raises(RuntimeError, match="Simulated DB failure"):
        svc.apply_payment_event(ev)

    monkeypatch.setattr(Session, "commit", original_commit)

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
    mock_payments = MagicMock(spec=PaymentsClient)
    mock_payments.cancel_order_payment.return_value = CancelPaymentResult(
        status="can_cancel", action="no_session"
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_payments
    try:
        client.post(
            f"/orders/{order1.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

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
        payments_client=mock_payments,
    )
    service.reconcile_expired_orders()
    db_session.refresh(order2)
    assert order2.status == "expired"
    events_expired = (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.order_id == order2.id)
        .all()
    )
    assert len(events_expired) == 0


def test_outbox_processor_publishes_to_rabbitmq_and_marks_published(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: OutboxProcessor публикува версия 1 конверт с event_id и отбелязва 'published'."""
    user, _ = _create_user_and_token(db_session, email="customer_pub@example.com")
    product = _create_product(db_session, "Publisher Prod", price_minor=7999)
    order = _create_order(db_session, user.id, product, quantity=1)

    fixed_event_id = str(uuid.uuid4())
    outbox_event = OrderOutboxEvent(
        event_id=fixed_event_id,
        event_version=1,
        order_id=order.id,
        event_type="order.paid",
        payload={
            "order_id": order.id,
            "customer_email": "customer_pub@example.com",
            "customer_phone": "+359888112233",
            "total_minor": 7999,
            "currency": "EUR",
        },
        status="pending",
    )
    db_session.add(outbox_event)
    db_session.commit()

    mock_publisher = MagicMock(spec=EventPublisher)

    processor = OutboxProcessor(
        session_factory=session_factory,
        publisher=mock_publisher,
    )
    stats = processor.process_pending_events()

    assert stats == {"processed": 1, "published": 1, "failed": 0}
    mock_publisher.publish_event.assert_called_once()
    call_kwargs = mock_publisher.publish_event.call_args.kwargs
    assert call_kwargs["routing_key"] == "order.paid"
    envelope = call_kwargs["envelope"]
    assert envelope["event_id"] == fixed_event_id
    assert envelope["event_type"] == "order.paid"
    assert envelope["event_version"] == 1
    assert envelope["producer"] == "commerce"
    assert envelope["occurred_at"]
    assert envelope["data"]["customer_phone"] == "+359888112233"

    with session_factory() as s:
        db_event = s.get(OrderOutboxEvent, outbox_event.id)
        assert db_event is not None
        assert db_event.status == "published"
        assert db_event.published_at is not None
        assert db_event.processed_at is not None
        assert db_event.retry_count == 0
        assert db_event.last_error is None


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: BrokerUnavailableError("RabbitMQ connection refused"),
        lambda: UnroutableEventError("NO_ROUTE returned by broker"),
        lambda: PublishConfirmLostError("Broker nacked confirm"),
    ],
)
def test_outbox_processor_keeps_pending_and_stable_event_id_on_publish_failures(
    db_session: Session,
    session_factory: sessionmaker[Session],
    error_factory,
) -> None:
    """При недостъпен брокер, липса на маршрут или загубено потвърждение event_id остава същият и се повтаря успешно."""
    user, _ = _create_user_and_token(db_session, email="fail_pub@example.com")
    product = _create_product(db_session, "Fail Publish", price_minor=5000)
    order = _create_order(db_session, user.id, product, status="paid")

    fixed_event_id = str(uuid.uuid4())
    outbox_event = OrderOutboxEvent(
        event_id=fixed_event_id,
        event_version=1,
        order_id=order.id,
        event_type="order.paid",
        payload={
            "order_id": order.id,
            "customer_email": "fail_pub@example.com",
            "customer_phone": "+359888999000",
            "total_minor": 5000,
            "currency": "EUR",
        },
        status="pending",
    )
    db_session.add(outbox_event)
    db_session.commit()

    failing_publisher = MagicMock(spec=EventPublisher)
    failing_publisher.publish_event.side_effect = error_factory()

    processor = OutboxProcessor(
        session_factory=session_factory,
        publisher=failing_publisher,
    )
    stats_fail = processor.process_pending_events()
    assert stats_fail == {"processed": 1, "published": 0, "failed": 1}

    with session_factory() as s:
        db_order = s.get(Order, order.id)
        assert db_order is not None
        assert db_order.status == "paid"

        db_event = s.get(OrderOutboxEvent, outbox_event.id)
        assert db_event is not None
        assert db_event.status == "pending"
        assert db_event.event_id == fixed_event_id
        assert db_event.published_at is None
        assert db_event.retry_count == 1
        assert db_event.last_error is not None

    # Възстановяване: вторият опит изпраща СЪЩИЯ event_id
    healthy_publisher = MagicMock(spec=EventPublisher)
    healthy_processor = OutboxProcessor(
        session_factory=session_factory,
        publisher=healthy_publisher,
    )
    stats_ok = healthy_processor.process_pending_events()
    assert stats_ok == {"processed": 1, "published": 1, "failed": 0}
    retried_envelope = healthy_publisher.publish_event.call_args.kwargs["envelope"]
    assert retried_envelope["event_id"] == fixed_event_id

    with session_factory() as s:
        db_event = s.get(OrderOutboxEvent, outbox_event.id)
        assert db_event is not None
        assert db_event.status == "published"
        assert db_event.published_at is not None
        assert db_event.retry_count == 1


@pytest.mark.parametrize("already_done_status", ["legacy_sent", "published"])
def test_outbox_processor_does_not_republish_legacy_sent_or_published(
    db_session: Session,
    session_factory: sessionmaker[Session],
    already_done_status: str,
) -> None:
    """Критерий: Исторически 'legacy_sent' и вече 'published' събития не се публикуват повторно."""
    user, _ = _create_user_and_token(db_session, email="idemp@example.com")
    product = _create_product(db_session, "Idemp RabbitMQ", price_minor=2000)
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
        status=already_done_status,
        processed_at=datetime.datetime.now(datetime.UTC),
    )
    db_session.add(outbox_event)
    db_session.commit()

    mock_publisher = MagicMock(spec=EventPublisher)
    processor = OutboxProcessor(
        session_factory=session_factory,
        publisher=mock_publisher,
    )
    stats = processor.process_pending_events()

    assert stats == {"processed": 0, "published": 0, "failed": 0}
    assert mock_publisher.publish_event.call_count == 0


def test_outbox_processor_never_logs_customer_contacts_and_logs_correlation_ids(
    db_session: Session,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Контактите на купувача не се извеждат в логовете или last_error; записват се order_id, shipment_id, event_id."""
    secret_email = "private.buyer.pii@example.com"
    secret_phone = "+359887654321"
    user, _ = _create_user_and_token(db_session, email=secret_email)
    product = _create_product(db_session, "PII Guard Prod", price_minor=1500)
    order = _create_order(db_session, user.id, product, status="paid")

    event_uuid = str(uuid.uuid4())
    ev = OrderOutboxEvent(
        event_id=event_uuid,
        event_version=1,
        order_id=order.id,
        shipment_id=None,
        event_type="order.paid",
        payload={
            "order_id": order.id,
            "customer_email": secret_email,
            "customer_phone": secret_phone,
            "total_minor": 1500,
            "currency": "EUR",
        },
        status="pending",
    )
    db_session.add(ev)
    db_session.commit()

    failing_publisher = MagicMock(spec=EventPublisher)
    # Дори ако външна грешка споменава контактите, те трябва да се санитизират
    failing_publisher.publish_event.side_effect = BrokerUnavailableError(
        f"Leak attempt {secret_email} {secret_phone}"
    )

    caplog.set_level(logging.INFO)
    processor = OutboxProcessor(
        session_factory=session_factory,
        publisher=failing_publisher,
    )
    processor.process_pending_events()

    # Успешен повторен опит
    ok_publisher = MagicMock(spec=EventPublisher)
    processor_ok = OutboxProcessor(
        session_factory=session_factory,
        publisher=ok_publisher,
    )
    processor_ok.process_pending_events()

    full_logs = caplog.text
    assert secret_email not in full_logs
    assert secret_phone not in full_logs
    assert event_uuid in full_logs
    assert f"order_id={order.id}" in full_logs
    assert "shipment_id=None" in full_logs

    with session_factory() as s:
        db_ev = s.get(OrderOutboxEvent, ev.id)
        assert db_ev is not None
        assert db_ev.last_error is None


def test_rabbitmq_publisher_raises_on_unroutable_nack_and_broker_unavailable() -> None:
    """Проверява, че RabbitMQEventPublisher не потвърждава при UnroutableError, NackError и срив на връзката."""
    publisher = RabbitMQEventPublisher(auto_declare_topology=False)
    envelope = {
        "event_id": str(uuid.uuid4()),
        "event_type": "order.paid",
        "event_version": 1,
        "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "producer": "commerce",
        "data": {"order_id": 1},
    }

    mock_channel = MagicMock()
    mock_channel.is_open = True
    mock_conn = MagicMock()
    mock_conn.is_open = True
    publisher._connection = mock_conn
    publisher._channel = mock_channel

    mock_channel.basic_publish.side_effect = pika.exceptions.UnroutableError([])
    with pytest.raises(UnroutableEventError):
        publisher.publish_event(routing_key="order.paid", envelope=envelope)

    mock_channel.basic_publish.side_effect = pika.exceptions.NackError([])
    with pytest.raises(PublishConfirmLostError):
        publisher.publish_event(routing_key="order.paid", envelope=envelope)

    mock_channel.basic_publish.side_effect = pika.exceptions.AMQPConnectionError("lost")
    with pytest.raises(BrokerUnavailableError):
        publisher.publish_event(routing_key="order.paid", envelope=envelope)


def test_outbox_database_constraints(db_session: Session) -> None:
    """Критерий: UNIQUE(event_id), UNIQUE(order_id, event_type) и CHECK(status) ограничения на outbox таблицата."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product)

    fixed_id = str(uuid.uuid4())
    ev1 = OrderOutboxEvent(
        event_id=fixed_id,
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

    # 2. Дублиран event_id -> UNIQUE(event_id)
    ev_dup_uuid = OrderOutboxEvent(
        event_id=fixed_id,
        order_id=order.id,
        event_type="other.event",
        payload={"order_id": order.id},
        status="pending",
    )
    db_session.add(ev_dup_uuid)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 3. Невалиден статус извън ('pending', 'legacy_sent', 'published', 'failed') -> CHECK ограничение
    ev_invalid_status = OrderOutboxEvent(
        order_id=order.id,
        event_type="order.shipped",
        payload={"order_id": order.id},
        status="sent",
    )
    db_session.add(ev_invalid_status)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
