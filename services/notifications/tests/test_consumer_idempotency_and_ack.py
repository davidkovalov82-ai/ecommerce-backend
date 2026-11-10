"""Тестове за идемпотентност в Notifications, БД ограничения и commit преди ack."""

import datetime
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.consumer import RabbitMQEventConsumer
from app.contracts import parse_and_validate_event
from app.models import InboxEvent, NotificationDelivery
from app.repository import NotificationRepository


def _make_paid_body(
    *, event_id: str | None = None, phone: str | None = "+359888111222"
) -> bytes:
    return json.dumps(
        {
            "event_id": event_id or str(uuid.uuid4()),
            "event_type": "order.paid",
            "event_version": 1,
            "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "producer": "commerce",
            "data": {
                "order_id": 101,
                "customer_email": "customer@example.com",
                "customer_phone": phone,
                "total_minor": 3200,
                "currency": "EUR",
            },
        }
    ).encode("utf-8")


def test_db_constraints_enforce_unique_event_and_channel(db_session: Session) -> None:
    """БД ограниченията забраняват >1 задание за двойка (event_id, channel) и невалидни статуси."""
    ev_id = str(uuid.uuid4())
    ev = InboxEvent(
        event_id=ev_id,
        event_type="order.paid",
        event_version=1,
        order_id=10,
        payload={"order_id": 10},
        occurred_at=datetime.datetime.now(datetime.UTC),
    )
    db_session.add(ev)
    db_session.commit()

    d1 = NotificationDelivery(event_id=ev_id, channel="email", status="pending")
    db_session.add(d1)
    db_session.commit()

    # Дубликат на (event_id, channel) -> IntegrityError от uq_notification_deliveries_event_channel
    d_dup = NotificationDelivery(event_id=ev_id, channel="email", status="pending")
    db_session.add(d_dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Невалиден канал -> CHECK constraint
    d_bad_chan = NotificationDelivery(event_id=ev_id, channel="push", status="pending")
    db_session.add(d_bad_chan)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Невалиден статус -> CHECK constraint
    d_bad_status = NotificationDelivery(event_id=ev_id, channel="sms", status="unknown")
    db_session.add(d_bad_status)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_consumer_commits_before_ack_and_survives_crash_before_ack(
    session_factory: sessionmaker[Session],
) -> None:
    """Notifications записва събитие и канали в 1 транзакция -> commit -> ack; повторна доставка не дублира."""
    event_id = str(uuid.uuid4())
    body = _make_paid_body(event_id=event_id, phone="+359888333444")

    # 1. Имитираме срив веднага след DB commit, преди basic_ack
    class _CrashBeforeAck(Exception):
        pass

    def _crash_hook(_ev, _ch, _method) -> None:
        with session_factory() as check_sess:
            # Вече е commit-нато в БД преди ack!
            assert check_sess.get(InboxEvent, event_id) is not None
        raise _CrashBeforeAck("connection dropped right before basic_ack")

    consumer_crash = RabbitMQEventConsumer(
        session_factory=session_factory,
        before_ack_hook=_crash_hook,
    )
    channel1 = MagicMock()
    method1 = SimpleNamespace(delivery_tag=1, redelivered=False)
    with pytest.raises(_CrashBeforeAck):
        consumer_crash._handle_message(channel1, method1, None, body)
    channel1.basic_ack.assert_not_called()

    # 2. Повторна доставка (redelivered=True) минава идемпотентно и изпраща ack
    consumer_ok = RabbitMQEventConsumer(session_factory=session_factory)
    channel2 = MagicMock()
    method2 = SimpleNamespace(delivery_tag=2, redelivered=True)
    consumer_ok._handle_message(channel2, method2, None, body)
    channel2.basic_ack.assert_called_once_with(delivery_tag=2)

    with session_factory() as s:
        deliveries = (
            s.execute(
                select(NotificationDelivery)
                .where(NotificationDelivery.event_id == event_id)
                .order_by(NotificationDelivery.channel)
            )
            .scalars()
            .all()
        )
        assert [(d.channel, d.status) for d in deliveries] == [
            ("email", "pending"),
            ("sms", "pending"),
        ]


def test_concurrent_redelivery_creates_single_task_per_channel(
    session_factory: sessionmaker[Session],
) -> None:
    """Паралелна повторна доставка на един и същ event_id оставя точно 1 запис на (event_id, channel)."""
    event_id = str(uuid.uuid4())
    validated = parse_and_validate_event(_make_paid_body(event_id=event_id))

    def _ingest_worker(_: int) -> None:
        with session_factory() as s:
            repo = NotificationRepository(s)
            repo.ingest_event(validated, email_max_attempts=5, sms_max_attempts=3)
            s.commit()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_ingest_worker, range(6)))

    with session_factory() as s:
        assert s.query(InboxEvent).filter(InboxEvent.event_id == event_id).count() == 1
        deliveries = (
            s.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_id == event_id
                )
            )
            .scalars()
            .all()
        )
        assert {d.channel for d in deliveries} == {"email", "sms"}
        assert len(deliveries) == 2
