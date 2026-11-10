"""Тестове на договорите на събитията и маршрутизирането към DLQ при грешка/непозната версия."""

import datetime
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import DataError, OperationalError

from app.consumer import RabbitMQEventConsumer
from app.contracts import (
    PG_BIGINT_MAX,
    PG_INT_MAX,
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
        (b"\xff\xfe", InvalidEventContract),
        (b"[]", InvalidEventContract),
        (b'{"a": ' + b"[" * 1500 + b"1" + b"]" * 1500 + b"}", InvalidEventContract),
        (b'{"event_version": NaN, "data": {}}', InvalidEventContract),
        (
            (
                '{"event_id": "'
                + str(uuid.uuid4())
                + '", "event_type": "order.paid", "event_version": 1, '
                '"occurred_at": "2026-11-10T00:00:00+00:00", '
                '"data": {"order_id": '
                + ("9" * 5000)
                + ', "customer_email": "buyer@example.com", "total_minor": 100, "currency": "EUR"}}'
            ).encode("utf-8"),
            InvalidEventContract,
        ),
        (
            (
                '{"event_id": "'
                + str(uuid.uuid4())
                + '", "event_type": "order.paid", "event_version": '
                + ("9" * 5000)
                + ', "data": {"order_id": 1}}'
            ).encode("utf-8"),
            InvalidEventContract,
        ),
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
        # Граници на PostgreSQL BIGINT / INT: 2**63 и 2**31 трябва да се отхвърлят
        (
            json.dumps(
                {
                    **_valid_order_paid_envelope(),
                    "data": {
                        "order_id": 2**63,
                        "customer_email": "buyer@example.com",
                        "total_minor": 100,
                        "currency": "EUR",
                    },
                }
            ).encode("utf-8"),
            InvalidEventContract,
        ),
        (
            json.dumps(
                {
                    **_valid_order_paid_envelope(),
                    "data": {
                        "order_id": PG_BIGINT_MAX,
                        "customer_email": "buyer@example.com",
                        "total_minor": 2**63,
                        "currency": "EUR",
                    },
                }
            ).encode("utf-8"),
            InvalidEventContract,
        ),
        (
            json.dumps(
                {
                    "event_id": str(uuid.uuid4()),
                    "event_type": "shipment.shipped",
                    "event_version": 1,
                    "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "data": {
                        "order_id": 10,
                        "shipment_id": 2**63,
                        "customer_email": "buyer@example.com",
                        "items": [
                            {"product_id": 1, "product_title": "X", "quantity": 1}
                        ],
                    },
                }
            ).encode("utf-8"),
            InvalidEventContract,
        ),
        (
            json.dumps(
                {
                    "event_id": str(uuid.uuid4()),
                    "event_type": "shipment.shipped",
                    "event_version": 1,
                    "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "data": {
                        "order_id": 10,
                        "shipment_id": 20,
                        "seller_id": PG_INT_MAX + 1,
                        "customer_email": "buyer@example.com",
                        "items": [
                            {"product_id": 1, "product_title": "X", "quantity": 1}
                        ],
                    },
                }
            ).encode("utf-8"),
            InvalidEventContract,
        ),
        (
            json.dumps(
                {
                    "event_id": str(uuid.uuid4()),
                    "event_type": "shipment.shipped",
                    "event_version": 1,
                    "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "data": {
                        "order_id": 10,
                        "shipment_id": 20,
                        "customer_email": "buyer@example.com",
                        "items": [
                            {
                                "product_id": PG_INT_MAX + 1,
                                "product_title": "X",
                                "quantity": 1,
                            }
                        ],
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


def test_max_valid_pg_bigint_order_id_succeeds() -> None:
    """Горната граница PG_BIGINT_MAX (2**63 - 1) е валидна."""
    payload = _valid_order_paid_envelope()
    payload["data"]["order_id"] = PG_BIGINT_MAX
    ev = parse_and_validate_event(json.dumps(payload).encode("utf-8"))
    assert ev.order_id == PG_BIGINT_MAX


def test_consumer_dead_letters_invalid_event_and_unknown_version(
    session_factory,
) -> None:
    """Невалидно събитие, order_id=2**63 или непозната версия се пращат към DLQ с requeue=False."""
    consumer = RabbitMQEventConsumer(session_factory=session_factory)
    overflow_order_payload = _valid_order_paid_envelope()
    overflow_order_payload["data"]["order_id"] = 2**63

    for bad_body in (
        b"{bad json",
        json.dumps(_valid_order_paid_envelope(version=99)).encode("utf-8"),
        json.dumps(
            {**_valid_order_paid_envelope(), "event_type": "order.weird"}
        ).encode("utf-8"),
        json.dumps(overflow_order_payload).encode("utf-8"),
    ):
        channel = MagicMock()
        method = SimpleNamespace(delivery_tag=77, redelivered=False)
        consumer._handle_message(channel, method, None, bad_body)

        channel.basic_nack.assert_called_once_with(delivery_tag=77, requeue=False)
        channel.basic_ack.assert_not_called()

    with session_factory() as s:
        assert s.query(InboxEvent).count() == 0


def test_invalid_message_never_logs_raw_header_fields_or_contact_markers(
    session_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Невалидно съобщение (вкл. обект с контактна метка вместо event_version) не изтича в логовете."""
    caplog.set_level(logging.DEBUG)

    marker_version_contact = "ver.leak.marker@example.com"
    marker_id_contact = "id.leak.marker@example.com"
    marker_type_contact = "+359888111999"

    poison_body = json.dumps(
        {
            "event_id": f"raw-{marker_id_contact}",
            "event_type": f"raw-type-{marker_type_contact}",
            "event_version": {
                "contact": marker_version_contact,
                "phone": marker_type_contact,
            },
            "data": {"order_id": 1},
        }
    ).encode("utf-8")

    consumer = RabbitMQEventConsumer(session_factory=session_factory)
    channel = MagicMock()
    method = SimpleNamespace(delivery_tag=55, redelivered=False)
    consumer._handle_message(channel, method, None, poison_body)

    channel.basic_nack.assert_called_once_with(delivery_tag=55, requeue=False)
    channel.basic_ack.assert_not_called()

    logs = caplog.text
    assert marker_version_contact not in logs
    assert marker_id_contact not in logs
    assert marker_type_contact not in logs
    assert "raw-" not in logs
    assert "invalid_event_version_type" in logs


def test_consumer_dead_letters_5000_digit_json_then_processes_next_valid_without_leaks(
    session_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Число от 5000 цифри (ValueError) отива в DLQ без теч в логовете; следващото валидно съобщение минава в същия consumer."""
    caplog.set_level(logging.DEBUG)
    poison_marker = "huge-5k-contact@example.com"
    huge_int_literal = "9" * 5000
    huge_json_body = (
        '{"event_id": "raw-5k-'
        + poison_marker
        + '", "event_type": "raw-type-+359888999000", '
        '"event_version": 1, '
        '"occurred_at": "2026-11-10T00:00:00+00:00", '
        '"data": {"order_id": '
        + huge_int_literal
        + ', "customer_email": "'
        + poison_marker
        + '", "total_minor": 100, "currency": "EUR"}}'
    ).encode("utf-8")

    valid_env = _valid_order_paid_envelope()
    valid_body = json.dumps(valid_env).encode("utf-8")

    consumer = RabbitMQEventConsumer(session_factory=session_factory)
    channel = MagicMock()

    # 1. Лошото 5000-цифрено JSON съобщение се отхвърля с requeue=False (DLQ)
    consumer._handle_message(
        channel,
        SimpleNamespace(delivery_tag=101, redelivered=False),
        None,
        huge_json_body,
    )
    channel.basic_nack.assert_called_once_with(delivery_tag=101, requeue=False)
    channel.basic_ack.assert_not_called()

    # 2. Следващото валидно съобщение се обработва успешно от същия consumer
    consumer._handle_message(
        channel,
        SimpleNamespace(delivery_tag=102, redelivered=False),
        None,
        valid_body,
    )
    channel.basic_ack.assert_called_once_with(delivery_tag=102)

    with session_factory() as s:
        events = s.query(InboxEvent).all()
        assert len(events) == 1
        assert events[0].event_id == valid_env["event_id"]

    logs = caplog.text
    assert "reason=invalid_json" in logs
    assert poison_marker not in logs
    assert "raw-" not in logs
    assert "+359888999000" not in logs
    assert "9" * 64 not in logs


def test_consumer_does_not_swallow_unexpected_internal_errors() -> None:
    """Неочаквани вътрешни грешки извън БД прекъсванията не се поглъщат тихо от consumer."""
    buggy_session = MagicMock()
    buggy_session.__enter__.return_value = buggy_session
    buggy_session.__exit__.return_value = False
    buggy_session.execute.side_effect = RuntimeError("unexpected programming bug")

    consumer = RabbitMQEventConsumer(session_factory=lambda: buggy_session)
    channel = MagicMock()

    with pytest.raises(RuntimeError, match="unexpected programming bug"):
        consumer._handle_message(
            channel,
            SimpleNamespace(delivery_tag=202, redelivered=False),
            None,
            json.dumps(_valid_order_paid_envelope()).encode("utf-8"),
        )

    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_not_called()


def test_consumer_nacks_with_requeue_on_temporary_postgres_error_and_dlq_on_data_error() -> (
    None
):
    """Временна недостъпност на PostgreSQL -> requeue=True; DataError -> DLQ (requeue=False)."""
    # 1. Временна недостъпност (OperationalError) -> остава повторяема грешка
    transient_session = MagicMock()
    transient_session.__enter__.return_value = transient_session
    transient_session.__exit__.return_value = False
    transient_session.execute.side_effect = OperationalError(
        "SELECT 1", {}, Exception("connection to server refused")
    )

    consumer_transient = RabbitMQEventConsumer(
        session_factory=lambda: transient_session
    )
    ch1 = MagicMock()
    consumer_transient._handle_message(
        ch1,
        SimpleNamespace(delivery_tag=88, redelivered=False),
        None,
        json.dumps(_valid_order_paid_envelope()).encode("utf-8"),
    )
    ch1.basic_nack.assert_called_once_with(delivery_tag=88, requeue=True)
    ch1.basic_ack.assert_not_called()

    # 2. Невалидна стойност на ниво БД (DataError) -> праща се в DLQ без безкраен цикъл
    data_err_session = MagicMock()
    data_err_session.__enter__.return_value = data_err_session
    data_err_session.__exit__.return_value = False
    data_err_session.execute.side_effect = DataError(
        "INSERT", {}, Exception("bigint out of range")
    )

    consumer_data_err = RabbitMQEventConsumer(session_factory=lambda: data_err_session)
    ch2 = MagicMock()
    consumer_data_err._handle_message(
        ch2,
        SimpleNamespace(delivery_tag=89, redelivered=False),
        None,
        json.dumps(_valid_order_paid_envelope()).encode("utf-8"),
    )
    ch2.basic_nack.assert_called_once_with(delivery_tag=89, requeue=False)
    ch2.basic_ack.assert_not_called()
