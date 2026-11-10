"""Тестове на договорите на събитията и маршрутизирането към DLQ при грешка/непозната версия."""

import datetime
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.consumer import RabbitMQEventConsumer
from app.contracts import (
    InvalidEventContract,
    UnsupportedEventType,
    UnsupportedEventVersion,
    parse_and_validate_event,
)
from app.models import InboxEvent


def _valid_order_paid_envelope(
    *,
    event_id: str | None = None,
    version: int = 1,
    phone: str | None = "+359888123456",
) -> dict:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": "order.paid",
        "event_version": version,
        "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "producer": "commerce",
        "data": {
            "order_id": 42,
            "customer_email": "buyer@example.com",
            "customer_phone": phone,
            "total_minor": 4500,
            "currency": "EUR",
        },
    }


def test_parse_valid_order_paid_and_shipment_shipped() -> None:
    paid_env = _valid_order_paid_envelope()
    ev = parse_and_validate_event(json.dumps(paid_env).encode("utf-8"))
    assert ev.event_id == paid_env["event_id"]
    assert ev.event_type == "order.paid"
    assert ev.event_version == 1
    assert ev.order_id == 42
    assert ev.shipment_id is None
    assert ev.customer_email == "buyer@example.com"
    assert ev.customer_phone == "+359888123456"

    shipped_env = {
        "event_id": str(uuid.uuid4()),
        "event_type": "shipment.shipped",
        "event_version": 1,
        "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "producer": "commerce",
        "data": {
            "order_id": 42,
            "shipment_id": 107,
            "seller_id": 5,
            "customer_email": "buyer@example.com",
            "customer_phone": None,
            "items": [
                {"product_id": 1, "product_title": "Слушалки", "quantity": 1},
            ],
        },
    }
    ev_ship = parse_and_validate_event(json.dumps(shipped_env).encode("utf-8"))
    assert ev_ship.event_type == "shipment.shipped"
    assert ev_ship.order_id == 42
    assert ev_ship.shipment_id == 107
    assert ev_ship.customer_phone is None
    assert len(ev_ship.payload["items"]) == 1


@pytest.mark.parametrize(
    ("raw_bytes", "exc_cls"),
    [
        (b"not-json{", InvalidEventContract),
        (b"[]", InvalidEventContract),
        (
            json.dumps(_valid_order_paid_envelope(version=2)).encode("utf-8"),
            UnsupportedEventVersion,
        ),
        (
            json.dumps(
                {
                    **_valid_order_paid_envelope(),
                    "event_version": True,
                }
            ).encode("utf-8"),
            InvalidEventContract,
        ),
        (
            json.dumps(
                {
                    **_valid_order_paid_envelope(),
                    "event_type": "order.unknown",
                }
            ).encode("utf-8"),
            UnsupportedEventType,
        ),
        (
            json.dumps(
                {
                    **_valid_order_paid_envelope(),
                    "event_id": "not-a-uuid",
                }
            ).encode("utf-8"),
            InvalidEventContract,
        ),
        (
            json.dumps(
                {
                    **_valid_order_paid_envelope(),
                    "data": {
                        "order_id": -1,
                        "customer_email": "bad-email",
                        "total_minor": 100,
                        "currency": "EUR",
                    },
                }
            ).encode("utf-8"),
            InvalidEventContract,
        ),
    ],
)
def test_invalid_or_unknown_version_raises_contract_error(
    raw_bytes: bytes,
    exc_cls: type[Exception],
) -> None:
    with pytest.raises(exc_cls):
        parse_and_validate_event(raw_bytes)


def test_consumer_dead_letters_invalid_event_and_unknown_version(
    session_factory,
) -> None:
    """Невалидно събитие или непозната версия се пращат към DLQ с basic_nack(requeue=False)."""
    consumer = RabbitMQEventConsumer(session_factory=session_factory)
    for bad_body in (
        b"{bad json",
        json.dumps(_valid_order_paid_envelope(version=99)).encode("utf-8"),
        json.dumps(
            {**_valid_order_paid_envelope(), "event_type": "order.weird"}
        ).encode("utf-8"),
    ):
        channel = MagicMock()
        method = SimpleNamespace(delivery_tag=77, redelivered=False)
        consumer._handle_message(channel, method, None, bad_body)

        channel.basic_nack.assert_called_once_with(delivery_tag=77, requeue=False)
        channel.basic_ack.assert_not_called()

    with session_factory() as s:
        assert s.query(InboxEvent).count() == 0


def test_consumer_nacks_with_requeue_on_db_error_before_commit() -> None:
    """При грешка в БД преди commit консуматорът НЕ прави ack, а nack(requeue=True)."""
    failing_session = MagicMock()
    failing_session.__enter__.return_value = failing_session
    failing_session.__exit__.return_value = False
    failing_session.commit.side_effect = RuntimeError("DB down before commit")

    consumer = RabbitMQEventConsumer(session_factory=lambda: failing_session)
    channel = MagicMock()
    method = SimpleNamespace(delivery_tag=88, redelivered=False)
    body = json.dumps(_valid_order_paid_envelope()).encode("utf-8")

    # Имитираме провал при ingest_event/commit
    failing_session.execute.side_effect = RuntimeError("DB connection lost")
    consumer._handle_message(channel, method, None, body)

    channel.basic_nack.assert_called_once_with(delivery_tag=88, requeue=True)
    channel.basic_ack.assert_not_called()
