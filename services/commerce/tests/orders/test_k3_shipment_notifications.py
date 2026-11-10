import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderOutboxEvent, Shipment
from app.modules.orders.outbox_processor import OutboxProcessor
from app.modules.orders.publisher import BrokerUnavailableError, EventPublisher
from tests.orders.helpers import (
    DEFAULT_SHIPPING_ADDRESS,
    _add_to_cart,
    _create_user_and_token,
)

client = TestClient(app)


def _setup_multi_seller_order(
    db_session: Session,
    customer_email: str,
) -> tuple[dict, dict, Order, Shipment, Shipment]:
    """Създава поръчка за купувач с двама продавачи (Анна и Борис)."""
    anna, anna_token = _create_user_and_token(
        db_session, email=f"anna_{uuid.uuid4().hex[:6]}@example.com"
    )
    boris, boris_token = _create_user_and_token(
        db_session, email=f"boris_{uuid.uuid4().hex[:6]}@example.com"
    )
    clara, clara_token = _create_user_and_token(db_session, email=customer_email)

    p1 = Product(
        seller_id=anna.id,
        title="Слушалки Sony",
        price_minor=5000,
        currency="EUR",
        stock=10,
        active=True,
    )
    p2 = Product(
        seller_id=anna.id,
        title="USB-C кабел",
        price_minor=1500,
        currency="EUR",
        stock=10,
        active=True,
    )
    p3 = Product(
        seller_id=boris.id,
        title="Книга Python",
        price_minor=2000,
        currency="EUR",
        stock=5,
        active=True,
    )
    db_session.add_all([p1, p2, p3])
    db_session.commit()

    _add_to_cart(db_session, clara.id, p1.id, quantity=1)
    _add_to_cart(db_session, clara.id, p2.id, quantity=2)
    _add_to_cart(db_session, clara.id, p3.id, quantity=1)

    headers = {
        "Authorization": f"Bearer {clara_token}",
        "Idempotency-Key": str(uuid.uuid4()),
    }
    payload = {"shipping_address": DEFAULT_SHIPPING_ADDRESS}
    res = client.post("/orders", json=payload, headers=headers)
    assert res.status_code == 201
    order_id = res.json()["id"]

    order = db_session.get(Order, order_id)
    assert order is not None
    order.status = "paid"
    db_session.commit()
    db_session.refresh(order)

    anna_s = next(s for s in order.shipments if s.seller_id == anna.id)
    boris_s = next(s for s in order.shipments if s.seller_id == boris.id)

    return (
        {"user": anna, "token": anna_token},
        {"user": boris, "token": boris_token},
        order,
        anna_s,
        boris_s,
    )


def test_shipment_shipped_atomic_outbox_transaction(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Ако записът на outbox събитието пропадне, преходът на пратката в shipped се отменя (rollback)."""
    customer_email = f"cust_{uuid.uuid4().hex[:8]}@example.com"
    anna_data, _, _order, anna_s, _ = _setup_multi_seller_order(
        db_session, customer_email
    )

    with (
        patch(
            "app.modules.orders.repository.OrderRepository.record_outbox_event",
            side_effect=RuntimeError("Simulated Outbox DB Failure"),
        ),
        pytest.raises(RuntimeError, match="Simulated Outbox DB Failure"),
    ):
        client.post(
            f"/seller/shipments/{anna_s.id}/ship",
            headers={"Authorization": f"Bearer {anna_data['token']}"},
        )

    with session_factory() as s:
        db_shipment = s.get(Shipment, anna_s.id)
        assert db_shipment is not None
        assert db_shipment.status == "unshipped"
        assert db_shipment.shipped_at is None

        events = (
            s.execute(
                select(OrderOutboxEvent).where(
                    OrderOutboxEvent.shipment_id == anna_s.id,
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 0


def test_shipment_events_have_isolated_items_and_stable_event_ids(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Анна и Борис изпращат своите пратки -> две отделни събития само със стоките на съответния продавач."""
    customer_email = f"clara_{uuid.uuid4().hex[:8]}@example.com"
    anna_data, boris_data, order, anna_s, boris_s = _setup_multi_seller_order(
        db_session, customer_email
    )

    published_calls: list[dict] = []

    class _RecordingPublisher:
        def publish_event(self, *, routing_key: str, envelope: dict) -> None:
            published_calls.append({"routing_key": routing_key, "envelope": envelope})

        def close(self) -> None:
            pass

    processor = OutboxProcessor(
        session_factory=session_factory,
        publisher=_RecordingPublisher(),
    )

    res1 = client.post(
        f"/seller/shipments/{anna_s.id}/ship",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert res1.status_code == 200

    res2 = client.post(
        f"/seller/shipments/{boris_s.id}/ship",
        headers={"Authorization": f"Bearer {boris_data['token']}"},
    )
    assert res2.status_code == 200

    stats = processor.process_pending_events(limit=10)
    assert stats == {"processed": 2, "published": 2, "failed": 0}
    assert len(published_calls) == 2

    ev1, ev2 = published_calls[0]["envelope"], published_calls[1]["envelope"]
    assert ev1["event_id"] != ev2["event_id"]
    assert uuid.UUID(ev1["event_id"])
    assert uuid.UUID(ev2["event_id"])
    assert ev1["event_version"] == 1
    assert ev2["event_version"] == 1
    assert ev1["data"]["order_id"] == order.id
    assert ev2["data"]["order_id"] == order.id
    assert ev1["data"]["shipment_id"] == anna_s.id
    assert ev2["data"]["shipment_id"] == boris_s.id

    titles1 = {item["product_title"] for item in ev1["data"]["items"]}
    titles2 = {item["product_title"] for item in ev2["data"]["items"]}
    assert titles1 == {"Слушалки Sony", "USB-C кабел"}
    assert titles2 == {"Книга Python"}
    assert ev1["data"]["customer_phone"] == DEFAULT_SHIPPING_ADDRESS["phone"]
    assert ev2["data"]["customer_phone"] == DEFAULT_SHIPPING_ADDRESS["phone"]


def test_shipment_publish_failure_keeps_shipped_and_retries_with_same_event_id(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """При срив на RabbitMQ пратката остава shipped, а повторното публикуване запазва същия event_id."""
    customer_email = f"clara_retry_{uuid.uuid4().hex[:8]}@example.com"
    anna_data, _, _order, anna_s, _ = _setup_multi_seller_order(
        db_session, customer_email
    )

    res = client.post(
        f"/seller/shipments/{anna_s.id}/ship",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert res.status_code == 200

    failing_publisher = MagicMock(spec=EventPublisher)
    failing_publisher.publish_event.side_effect = BrokerUnavailableError(
        "RabbitMQ unreachable"
    )
    broken_processor = OutboxProcessor(
        session_factory=session_factory,
        publisher=failing_publisher,
    )

    stats_failed = broken_processor.process_pending_events(limit=10)
    assert stats_failed == {"processed": 1, "published": 0, "failed": 1}

    db_session.refresh(anna_s)
    assert anna_s.status == "shipped"

    event = (
        db_session.execute(
            select(OrderOutboxEvent).where(
                OrderOutboxEvent.shipment_id == anna_s.id,
            )
        )
        .scalars()
        .first()
    )
    assert event is not None
    assert event.status == "pending"
    assert event.retry_count == 1
    assert event.last_error is not None
    stable_event_id = event.event_id

    healthy_publisher = MagicMock(spec=EventPublisher)
    healthy_processor = OutboxProcessor(
        session_factory=session_factory,
        publisher=healthy_publisher,
    )
    stats_ok = healthy_processor.process_pending_events(limit=10)
    assert stats_ok == {"processed": 1, "published": 1, "failed": 0}
    sent_envelope = healthy_publisher.publish_event.call_args.kwargs["envelope"]
    assert sent_envelope["event_id"] == stable_event_id

    db_session.refresh(event)
    assert event.status == "published"
    assert event.published_at is not None

    stats_third = healthy_processor.process_pending_events(limit=10)
    assert stats_third == {"processed": 0, "published": 0, "failed": 0}
