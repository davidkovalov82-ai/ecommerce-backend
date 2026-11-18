import datetime
import json
import logging
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import DataError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.modules.orders.payment_consumer import RabbitMQPaymentEventConsumer
from app.modules.orders.payment_contracts import (
    InvalidPaymentContract,
    UnsupportedPaymentEventType,
    UnsupportedPaymentEventVersion,
    parse_and_validate_payment_event,
)
from tests.payments.helpers import make_payment_event_envelope


def test_parse_valid_payment_event_contract() -> None:
    """Валидно събитие преминава контракта с точни типове и нормализирана валута."""
    envelope = make_payment_event_envelope(
        event_type="payment.succeeded",
        order_id=10,
        amount_minor=2500,
        currency="eur",
    )
    parsed = parse_and_validate_payment_event(json.dumps(envelope).encode("utf-8"))
    assert parsed.event_id == envelope["event_id"]
    assert parsed.event_type == "payment.succeeded"
    assert parsed.order_id == 10
    assert parsed.currency == "EUR"
    assert parsed.amount_minor == 2500


def test_parse_rejects_invalid_json_5000_digits_and_constants() -> None:
    """Невалиден JSON, NaN/Infinity и число с 5000 цифри се превръщат в InvalidPaymentContract."""
    for bad in (b"{broken json", b"[]", b"\xff\xfe", b'{"x": NaN}'):
        with pytest.raises(InvalidPaymentContract):
            parse_and_validate_payment_event(bad)

    huge_digits = "9" * (getattr(sys, "get_int_max_str_digits", lambda: 4300)() + 700)
    bomb_payload = (
        f'{{"event_id": "{uuid.uuid4()}", "event_type": "payment.succeeded", '
        f'"event_version": {huge_digits}, "occurred_at": "2026-01-01T00:00:00Z", '
        f'"producer": "payments", "data": {{"order_id": 1}}}}'
    ).encode()
    with pytest.raises(InvalidPaymentContract):
        parse_and_validate_payment_event(bomb_payload)


def test_parse_rejects_pg_int_overflow_bool_version_and_unsupported_types() -> None:
    """Граничните стойности 2**63, bool версия и непознати версии/типове отиват в DLQ."""
    # order_id = 2**63 (препълва PG BIGINT)
    env_overflow = make_payment_event_envelope(
        event_type="payment.succeeded",
        order_id=2**63,
    )
    with pytest.raises(InvalidPaymentContract):
        parse_and_validate_payment_event(json.dumps(env_overflow).encode("utf-8"))

    # bool като event_version
    env_bool_ver = make_payment_event_envelope(
        event_type="payment.succeeded",
        order_id=1,
    )
    env_bool_ver["event_version"] = True
    with pytest.raises(InvalidPaymentContract):
        parse_and_validate_payment_event(json.dumps(env_bool_ver).encode("utf-8"))

    # Неподдържана версия
    env_ver2 = make_payment_event_envelope(
        event_type="payment.succeeded",
        order_id=1,
        event_version=99,
    )
    with pytest.raises(UnsupportedPaymentEventVersion):
        parse_and_validate_payment_event(json.dumps(env_ver2).encode("utf-8"))

    # Поддържани типове включват payment.cancelled (със и без payment_attempt_id)
    env_cancelled = make_payment_event_envelope(
        event_type="payment.cancelled",
        order_id=1,
    )
    assert (
        parse_and_validate_payment_event(
            json.dumps(env_cancelled).encode("utf-8")
        ).event_type
        == "payment.cancelled"
    )
    env_cancelled_no_attempt = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment.cancelled",
        "event_version": 1,
        "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "producer": "payments",
        "data": {
            "order_id": 2,
            "payment_attempt_id": None,
            "attempt_key": None,
            "closure_reason": "cancel",
        },
    }
    parsed_no_attempt = parse_and_validate_payment_event(
        json.dumps(env_cancelled_no_attempt).encode("utf-8")
    )
    assert parsed_no_attempt.event_type == "payment.cancelled"
    assert parsed_no_attempt.order_id == 2
    assert parsed_no_attempt.payment_attempt_id is None

    # payment.succeeded с payment_attempt_id=None е невалидно -> DLQ
    env_bad_succ = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment.succeeded",
        "event_version": 1,
        "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "producer": "payments",
        "data": {"order_id": 2, "payment_attempt_id": None},
    }
    with pytest.raises(InvalidPaymentContract):
        parse_and_validate_payment_event(json.dumps(env_bad_succ).encode("utf-8"))

    # Неподдържан тип
    env_bad_type = make_payment_event_envelope(
        event_type="payment.refunded",
        order_id=1,
    )
    with pytest.raises(UnsupportedPaymentEventType):
        parse_and_validate_payment_event(json.dumps(env_bad_type).encode("utf-8"))


def test_consumer_dead_letters_bad_message_without_logging_raw_secrets(
    session_factory: sessionmaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Невалидното съобщение получава basic_nack(requeue=False) без изтичане на сурови данни в лога."""
    consumer = RabbitMQPaymentEventConsumer(session_factory=session_factory)
    channel = MagicMock()
    method = SimpleNamespace(delivery_tag=17)

    secret_marker = "pii_secret_contact_marker@example.com"
    bad_payload = json.dumps(
        {
            "event_id": "not-a-uuid",
            "event_type": secret_marker,
            "event_version": 1,
            "occurred_at": "2026-01-01T00:00:00Z",
            "producer": "payments",
            "data": {"secret_tag": secret_marker},
        }
    ).encode("utf-8")

    with caplog.at_level(logging.WARNING):
        consumer._handle_message(channel, method, None, bad_payload)

    channel.basic_nack.assert_called_once_with(delivery_tag=17, requeue=False)
    channel.basic_ack.assert_not_called()
    assert secret_marker not in caplog.text


def test_consumer_distinguishes_transient_db_error_vs_data_error(
    session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OperationalError -> requeue=True, DataError -> DLQ (requeue=False)."""
    consumer = RabbitMQPaymentEventConsumer(session_factory=session_factory)
    payload = json.dumps(
        make_payment_event_envelope(event_type="payment.succeeded", order_id=1)
    ).encode("utf-8")

    # 1. Временна DB грешка -> requeue=True
    def raise_operational(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("stmt", {}, Exception("pg temporarily unavailable"))

    monkeypatch.setattr(
        "app.modules.orders.service.OrderService.apply_payment_event",
        raise_operational,
    )
    ch1 = MagicMock()
    consumer._handle_message(ch1, SimpleNamespace(delivery_tag=1), None, payload)
    ch1.basic_nack.assert_called_once_with(delivery_tag=1, requeue=True)
    ch1.basic_ack.assert_not_called()

    # 2. Трайна DataError -> DLQ (requeue=False)
    def raise_data_error(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise DataError("stmt", {}, Exception("value out of range"))

    monkeypatch.setattr(
        "app.modules.orders.service.OrderService.apply_payment_event",
        raise_data_error,
    )
    ch2 = MagicMock()
    consumer._handle_message(ch2, SimpleNamespace(delivery_tag=2), None, payload)
    ch2.basic_nack.assert_called_once_with(delivery_tag=2, requeue=False)
    ch2.basic_ack.assert_not_called()
