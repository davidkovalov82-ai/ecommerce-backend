import json
import time
import urllib.request
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.main import app
from app.modules.catalog.models import Product
from app.modules.orders.mailer import EmailSender
from app.modules.orders.models import Order, OrderOutboxEvent, Shipment
from app.modules.orders.outbox_processor import OutboxProcessor
from tests.orders.helpers import (
    DEFAULT_SHIPPING_ADDRESS,
    _add_to_cart,
    _create_user_and_token,
)

client = TestClient(app)


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

    # Симулираме грешка при запис на outbox събитие в OrderRepository.record_outbox_event
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

    # Проверка през отделна сесия след rollback
    with session_factory() as s:
        db_shipment = s.get(Shipment, anna_s.id)
        assert db_shipment is not None
        assert db_shipment.status == "unshipped"
        assert db_shipment.shipped_at is None

        # В outbox няма записано събитие за тази пратка
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


def test_shipment_notifications_live_mailpit(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """E2E: Анна и Борис изпращат последователно. Купувачът получава 2 отделни писма,
    всяко съдържащо САМО артикулите на съответния продавач.
    """
    assert _is_mailpit_live(), "Mailpit услугата не е достъпна на http://127.0.0.1:8025"

    customer_email = f"clara_{uuid.uuid4().hex[:8]}@example.com"
    anna_data, boris_data, order, anna_s, boris_s = _setup_multi_seller_order(
        db_session, customer_email
    )

    email_sender = EmailSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        from_email=settings.smtp_from,
    )
    processor = OutboxProcessor(
        session_factory=session_factory,
        email_sender=email_sender,
    )

    # 1. Анна изпраща пратката си
    res1 = client.post(
        f"/seller/shipments/{anna_s.id}/ship",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert res1.status_code == 200

    # Обработваме outbox събитието
    stats1 = processor.process_pending_events(limit=10)
    assert stats1["sent"] >= 1

    # Купувачът получава първо писмо
    msgs1 = _wait_for_messages_for_recipient(customer_email, expected_count=1)
    assert len(msgs1) == 1
    m1_detail = _get_message_detail(msgs1[0]["ID"])
    m1_text = m1_detail.get("Text", "") + m1_detail.get("Snippet", "")
    assert str(order.id) in m1_detail["Subject"]
    assert "Слушалки Sony" in m1_text
    assert "USB-C кабел" in m1_text
    # Артикулът на Борис НЕ трябва да фигурира в известието за пратката на Анна
    assert "Книга Python" not in m1_text

    # 2. Борис изпраща пратката си
    res2 = client.post(
        f"/seller/shipments/{boris_s.id}/ship",
        headers={"Authorization": f"Bearer {boris_data['token']}"},
    )
    assert res2.status_code == 200

    # Обработваме второто outbox събитие
    stats2 = processor.process_pending_events(limit=10)
    assert stats2["sent"] >= 1

    # Купувачът получава второ писмо (общо 2)
    msgs2 = _wait_for_messages_for_recipient(customer_email, expected_count=2)
    assert len(msgs2) == 2

    # Намираме новото съобщение (различно от първото по ID)
    m2_meta = next(m for m in msgs2 if m["ID"] != msgs1[0]["ID"])
    m2_detail = _get_message_detail(m2_meta["ID"])
    m2_text = m2_detail.get("Text", "") + m2_detail.get("Snippet", "")
    assert str(order.id) in m2_detail["Subject"]
    assert "Книга Python" in m2_text
    # Артикулите на Анна НЕ трябва да фигурират в известието за пратката на Борис
    assert "Слушалки Sony" not in m2_text


def test_shipment_smtp_failure_and_retry(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """При SMTP срив пратката остава shipped, а outbox събитието се предава успешно при възстановяване на SMTP."""
    assert _is_mailpit_live(), "Mailpit услугата не е достъпна на http://127.0.0.1:8025"

    customer_email = f"clara_retry_{uuid.uuid4().hex[:8]}@example.com"
    anna_data, _, _order, anna_s, _ = _setup_multi_seller_order(
        db_session, customer_email
    )

    # 1. Анна изпраща пратката
    res = client.post(
        f"/seller/shipments/{anna_s.id}/ship",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert res.status_code == 200

    # 2. Обработка със симулиран невалиден SMTP порт (срив на мрежата/SMTP)
    broken_sender = EmailSender(
        host=settings.smtp_host,
        port=65432,  # Несъществуващ порт
        from_email=settings.smtp_from,
        timeout=1.0,
    )
    broken_processor = OutboxProcessor(
        session_factory=session_factory,
        email_sender=broken_sender,
    )

    # Пускаме обработката: събитието опитва изпращане и пропада
    stats_failed = broken_processor.process_pending_events(limit=10)
    assert stats_failed["failed"] >= 1
    assert stats_failed["sent"] == 0

    # Проверка: пратката ОСТАВА shipped в БД, бизнес транзакцията не е засегната
    db_session.refresh(anna_s)
    assert anna_s.status == "shipped"

    # Outbox събитието е в pending с retry_count == 1
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

    # Купувачът още не е получил писмо
    assert len(_get_messages_for_recipient(customer_email)) == 0

    # 3. Възстановяване на SMTP (валиден sender)
    healthy_sender = EmailSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        from_email=settings.smtp_from,
    )
    healthy_processor = OutboxProcessor(
        session_factory=session_factory,
        email_sender=healthy_sender,
    )

    # Обработката се повтаря успешно
    stats_ok = healthy_processor.process_pending_events(limit=10)
    assert stats_ok["sent"] >= 1

    # Събитието в базата е маркирано като sent
    db_session.refresh(event)
    assert event.status == "sent"
    assert event.processed_at is not None

    # Писмото е успешно доставено в Mailpit
    msgs = _wait_for_messages_for_recipient(customer_email, expected_count=1)
    assert len(msgs) == 1

    # 4. Трето пускане (идемпотентност): не се изпраща второ писмо
    stats_third = healthy_processor.process_pending_events(limit=10)
    assert stats_third["sent"] == 0
    msgs_after = _get_messages_for_recipient(customer_email)
    assert len(msgs_after) == 1
