"""Тестове за липса на клиентски контакти в логовете и наличие на корелационни ID-та."""

import datetime
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.consumer import RabbitMQEventConsumer
from app.email_sender import EmailSenderProtocol
from app.models import NotificationDelivery
from app.sms_sender import SmsDeliveryError, SmsSenderProtocol
from app.worker import DeliveryWorker


def test_logs_never_leak_contacts_and_always_include_correlation_ids(
    session_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Проверява, че имейл и телефон липсват в логовете, а event_id/order_id/shipment_id присъстват."""
    caplog.set_level(logging.INFO)

    secret_email = "topsecret.customer@example.com"
    secret_phone = "+359888765432"
    event_id = str(uuid.uuid4())
    order_id = 5551
    shipment_id = 8882

    body = json.dumps(
        {
            "event_id": event_id,
            "event_type": "shipment.shipped",
            "event_version": 1,
            "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "producer": "commerce",
            "data": {
                "order_id": order_id,
                "shipment_id": shipment_id,
                "seller_id": 9,
                "customer_email": secret_email,
                "customer_phone": secret_phone,
                "items": [{"product_id": 1, "product_title": "Кабел", "quantity": 1}],
            },
        }
    ).encode("utf-8")

    consumer = RabbitMQEventConsumer(session_factory=session_factory)
    ch = MagicMock()
    consumer._handle_message(
        ch, SimpleNamespace(delivery_tag=1, redelivered=False), None, body
    )

    # Симулираме грешка с изтекъл PII от външен клиент -> трябва да се санитизира
    email_mock = MagicMock(spec=EmailSenderProtocol)
    email_mock.send_shipment_shipped_email.side_effect = RuntimeError(
        f"SMTP rejected recipient {secret_email}"
    )
    sms_mock = MagicMock(spec=SmsSenderProtocol)
    sms_mock.send_sms.side_effect = SmsDeliveryError(
        f"Provider error for {secret_phone}"
    )

    worker = DeliveryWorker(
        session_factory=session_factory,
        email_sender=email_mock,
        sms_sender=sms_mock,
        email_retry_delay_seconds=0,
        sms_retry_delay_seconds=0,
    )
    worker.process_once()

    # Повторно стартиране с успешен SMTP за проверка и на INFO логовете при успех
    email_mock.send_shipment_shipped_email.side_effect = None
    worker.process_once()

    logs = caplog.text
    assert secret_email not in logs
    assert secret_phone not in logs
    assert f"event_id={event_id}" in logs
    assert f"order_id={order_id}" in logs
    assert f"shipment_id={shipment_id}" in logs

    # Проверяваме и записаните last_error в БД да не съдържат контакти
    with session_factory() as s:
        deliveries = s.query(NotificationDelivery).all()
        for d in deliveries:
            if d.last_error:
                assert secret_email not in d.last_error
                assert secret_phone not in d.last_error
