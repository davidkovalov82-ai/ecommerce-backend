"""Тестове на DeliveryWorker: разделни канали, независимо повторение, crash recovery и липса на телефон."""

import datetime
import json
import threading
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.contracts import parse_and_validate_event
from app.email_sender import EmailSender, EmailSenderProtocol
from app.models import NotificationDelivery
from app.repository import NotificationRepository
from app.sms_sender import SmsDeliveryError, SmsSenderProtocol, SmsSendResult
from app.worker import DeliveryWorker


def _ingest_paid(
    session_factory: sessionmaker,
    *,
    order_id: int = 777,
    phone: str | None = "+359888123456",
    email: str | None = "buyer@example.com",
    email_max_attempts: int = 5,
    sms_max_attempts: int = 3,
    now: datetime.datetime | None = None,
) -> str:
    event_id = str(uuid.uuid4())
    data: dict[str, object] = {
        "order_id": order_id,
        "customer_email": email,
        "total_minor": 4500,
        "currency": "EUR",
    }
    if phone is not None:
        data["customer_phone"] = phone

    body = json.dumps(
        {
            "event_id": event_id,
            "event_type": "order.paid",
            "event_version": 1,
            "occurred_at": (now or datetime.datetime.now(datetime.UTC)).isoformat(),
            "producer": "commerce",
            "data": data,
        }
    ).encode("utf-8")
    ev = parse_and_validate_event(body)
    with session_factory() as s:
        NotificationRepository(s).ingest_event(
            ev,
            email_max_attempts=email_max_attempts,
            sms_max_attempts=sms_max_attempts,
            now=now,
        )
        s.commit()
    return event_id


def test_smtp_failure_delivers_sms_and_only_retries_email(
    session_factory: sessionmaker,
) -> None:
    """Грешка на SMTP не повтаря успешното SMS; след възстановяване се повтаря само email."""
    t0 = datetime.datetime(2026, 11, 10, 10, 0, 0, tzinfo=datetime.UTC)
    event_id = _ingest_paid(session_factory, phone="+359888555666", now=t0)

    email_mock = MagicMock(spec=EmailSenderProtocol)
    email_mock.send_order_paid_email.side_effect = ConnectionRefusedError("SMTP down")
    sms_mock = MagicMock(spec=SmsSenderProtocol)
    sms_mock.send_sms.return_value = SmsSendResult(
        provider="twilio", provider_message_id="SM_OK_1", status="queued"
    )

    worker = DeliveryWorker(
        session_factory=session_factory,
        email_sender=email_mock,
        sms_sender=sms_mock,
        email_retry_delay_seconds=10,
        sms_retry_delay_seconds=2,
        delivery_lease_seconds=60,
    )

    stats1 = worker.process_once(now=t0)
    assert stats1 == {"claimed": 2, "sent": 1, "retried": 1, "failed": 0, "stale": 0}
    assert sms_mock.send_sms.call_count == 1
    assert email_mock.send_order_paid_email.call_count == 1

    # Преди изтичане на email_retry_delay_seconds (10s): никой канал не се изпълнява
    stats_too_early = worker.process_once(now=t0 + datetime.timedelta(seconds=5))
    assert stats_too_early["claimed"] == 0

    # Възстановяваме SMTP и минаваме отвъд забавянето на email канала
    email_mock.send_order_paid_email.side_effect = None
    stats2 = worker.process_once(now=t0 + datetime.timedelta(seconds=11))
    assert stats2 == {"claimed": 1, "sent": 1, "retried": 0, "failed": 0, "stale": 0}

    # SMS НЕ е повторен!
    assert sms_mock.send_sms.call_count == 1
    assert email_mock.send_order_paid_email.call_count == 2

    with session_factory() as s:
        deliveries = {
            d.channel: d
            for d in s.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_id == event_id
                )
            )
            .scalars()
            .all()
        }
        assert deliveries["email"].status == "sent"
        assert deliveries["email"].attempt_count == 2
        assert deliveries["sms"].status == "sent"
        assert deliveries["sms"].attempt_count == 1


def test_legacy_event_without_phone_delivers_email_and_marks_sms_skipped(
    session_factory: sessionmaker,
) -> None:
    """Старо събитие без телефон: email се доставя, а SMS се отбелязва като 'skipped' с видима причина."""
    t0 = datetime.datetime(2026, 11, 10, 10, 0, 0, tzinfo=datetime.UTC)
    event_id = _ingest_paid(session_factory, phone=None, now=t0)

    email_mock = MagicMock(spec=EmailSenderProtocol)
    sms_mock = MagicMock(spec=SmsSenderProtocol)

    worker = DeliveryWorker(
        session_factory=session_factory,
        email_sender=email_mock,
        sms_sender=sms_mock,
    )
    stats = worker.process_once(now=t0)
    assert stats["sent"] == 1
    assert email_mock.send_order_paid_email.call_count == 1
    assert sms_mock.send_sms.call_count == 0

    with session_factory() as s:
        deliveries = {
            d.channel: d
            for d in s.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_id == event_id
                )
            )
            .scalars()
            .all()
        }
        assert deliveries["email"].status == "sent"
        assert deliveries["sms"].status == "skipped"
        assert deliveries["sms"].last_error == "missing_customer_phone"


def test_worker_crash_during_processing_recovers_after_lease_expiry(
    session_factory: sessionmaker,
) -> None:
    """Ако worker падне по време на обработка ('processing'), след изтичане на lease друг worker довършва задачата."""
    t0 = datetime.datetime(2026, 11, 10, 10, 0, 0, tzinfo=datetime.UTC)
    event_id = _ingest_paid(session_factory, phone=None, now=t0)

    # 1. Worker A заключва задачата (lease = 30s) и 'пада' преди финализиране
    with session_factory() as s:
        repo = NotificationRepository(s)
        claimed_a = repo.claim_due_deliveries(limit=10, lease_seconds=30, now=t0)
        s.commit()
    assert len(claimed_a) == 1
    stale_claim = claimed_a[0]
    assert stale_claim.channel == "email"

    # 2. В рамките на lease (t0 + 15s) нов worker не отнема задачата
    email_mock = MagicMock(spec=EmailSenderProtocol)
    sms_mock = MagicMock(spec=SmsSenderProtocol)
    worker_b = DeliveryWorker(
        session_factory=session_factory,
        email_sender=email_mock,
        sms_sender=sms_mock,
        delivery_lease_seconds=30,
    )
    stats_mid = worker_b.process_once(now=t0 + datetime.timedelta(seconds=15))
    assert stats_mid["claimed"] == 0

    # 3. След изтичане на lease (t0 + 31s) worker B я поема и завършва успешно
    stats_after = worker_b.process_once(now=t0 + datetime.timedelta(seconds=31))
    assert stats_after == {
        "claimed": 1,
        "sent": 1,
        "retried": 0,
        "failed": 0,
        "stale": 0,
    }
    assert email_mock.send_order_paid_email.call_count == 1

    # 4. Закъснял finalize от стария processing_token не поврежда състоянието
    with session_factory() as s:
        stale_outcome = NotificationRepository(s).finalize_delivery_failure(
            stale_claim,
            error_message="late timeout",
            retry_delay_seconds=5,
            now=t0 + datetime.timedelta(seconds=40),
        )
        s.commit()
        assert stale_outcome == "stale"

        email_row = s.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.event_id == event_id,
                NotificationDelivery.channel == "email",
            )
        ).scalar_one()
        assert email_row.status == "sent"
        assert email_row.attempt_count == 2


def test_channel_max_attempts_transitions_to_failed_independently(
    session_factory: sessionmaker,
) -> None:
    """Всеки канал спазва собствен лимит опити и преминава към терминален статус 'failed'."""
    t0 = datetime.datetime(2026, 11, 10, 10, 0, 0, tzinfo=datetime.UTC)
    event_id = _ingest_paid(
        session_factory,
        phone="+359888999111",
        email_max_attempts=2,
        sms_max_attempts=3,
        now=t0,
    )

    email_mock = MagicMock(spec=EmailSenderProtocol)
    sms_mock = MagicMock(spec=SmsSenderProtocol)
    sms_mock.send_sms.side_effect = SmsDeliveryError("twilio_http_503")

    worker = DeliveryWorker(
        session_factory=session_factory,
        email_sender=email_mock,
        sms_sender=sms_mock,
        email_retry_delay_seconds=1,
        sms_retry_delay_seconds=2,
    )

    worker.process_once(now=t0)  # email sent, sms retry 1
    worker.process_once(now=t0 + datetime.timedelta(seconds=3))  # sms retry 2
    worker.process_once(now=t0 + datetime.timedelta(seconds=6))  # sms fail (3/3)

    with session_factory() as s:
        rows = {
            d.channel: d
            for d in s.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_id == event_id
                )
            )
            .scalars()
            .all()
        }
        assert rows["email"].status == "sent"
        assert rows["sms"].status == "failed"
        assert rows["sms"].attempt_count == 3


def test_two_workers_with_slow_first_delivery_no_duplicate_sends(
    session_factory: sessionmaker,
) -> None:
    """Детерминистичен тест с 2 воркъра и бавна първа доставка: без повторно пращане."""
    t0 = datetime.datetime(2026, 11, 10, 10, 0, 0, tzinfo=datetime.UTC)
    ev1 = _ingest_paid(session_factory, order_id=1001, phone=None, now=t0)
    ev2 = _ingest_paid(session_factory, order_id=1002, phone=None, now=t0)

    # 1. Първа проверка: два паралелни воркъра по време на бавна първа доставка (Worker 1 държи 1001, Worker 2 поема 1002)
    task1_started = threading.Event()
    allow_task1_finish = threading.Event()
    sent_orders: list[int] = []
    sent_lock = threading.Lock()

    class _SlowEmailSenderWorker1:
        timeout = 2.0

        def send_order_paid_email(self, *, order_id: int, **_kwargs: object) -> None:
            if order_id == 1001:
                task1_started.set()
                assert allow_task1_finish.wait(timeout=5.0)
            with sent_lock:
                sent_orders.append(order_id)

        def send_shipment_shipped_email(self, **_kwargs: object) -> None:
            pass

    class _FastEmailSenderWorker2:
        timeout = 2.0

        def send_order_paid_email(self, *, order_id: int, **_kwargs: object) -> None:
            with sent_lock:
                sent_orders.append(order_id)

        def send_shipment_shipped_email(self, **_kwargs: object) -> None:
            pass

    w1_clock_state = [t0]
    w1 = DeliveryWorker(
        session_factory=session_factory,
        email_sender=_SlowEmailSenderWorker1(),
        sms_sender=MagicMock(spec=SmsSenderProtocol),
        delivery_lease_seconds=10,
        clock=lambda: w1_clock_state[0],
    )
    w2 = DeliveryWorker(
        session_factory=session_factory,
        email_sender=_FastEmailSenderWorker2(),
        sms_sender=MagicMock(spec=SmsSenderProtocol),
        delivery_lease_seconds=10,
        clock=lambda: t0 + datetime.timedelta(seconds=5),
    )

    w1_stats: dict[str, int] = {}

    def _run_w1() -> None:
        w1_stats.update(w1.process_once(limit=20))

    t_w1 = threading.Thread(target=_run_w1)
    t_w1.start()
    assert task1_started.wait(timeout=5.0)

    # Докато Worker 1 обработва бавната #1001, Worker 2 поема само #1002 (не пипа #1001)
    w2_stats = w2.process_once(limit=20)
    assert w2_stats["claimed"] == 1
    assert w2_stats["sent"] == 1

    allow_task1_finish.set()
    t_w1.join(timeout=5.0)
    assert not t_w1.is_alive()
    assert w1_stats["claimed"] == 1
    assert w1_stats["sent"] == 1
    assert sorted(sent_orders) == [1001, 1002]

    # 2. Втора проверка: последователен захват на партида с бавна първа доставка (9s при lease=10s)
    # Доказва, че втората задача получава пресен lease при започването си, а не изтекъл от t0+10s
    ev3 = _ingest_paid(session_factory, order_id=1003, phone=None, now=t0)
    ev4 = _ingest_paid(session_factory, order_id=1004, phone=None, now=t0)

    seq_clock = [t0]
    task4_started = threading.Event()
    allow_task4_finish = threading.Event()
    seq_sent_orders: list[int] = []

    class _SeqSlowSender:
        timeout = 2.0

        def send_order_paid_email(self, *, order_id: int, **_kwargs: object) -> None:
            if order_id == 1003:
                # Бавна първа доставка отнема 9s (при lease=10s). При стар партиден захват
                # на 1004 му оставаше само 1s lease и на 11-тата секунда щеше да бъде откраднат!
                seq_clock[0] = t0 + datetime.timedelta(seconds=9)
            elif order_id == 1004:
                seq_clock[0] = t0 + datetime.timedelta(seconds=11)
                task4_started.set()
                assert allow_task4_finish.wait(timeout=5.0)
            with sent_lock:
                seq_sent_orders.append(order_id)

        def send_shipment_shipped_email(self, **_kwargs: object) -> None:
            pass

    w_seq = DeliveryWorker(
        session_factory=session_factory,
        email_sender=_SeqSlowSender(),
        sms_sender=MagicMock(spec=SmsSenderProtocol),
        delivery_lease_seconds=10,
        clock=lambda: seq_clock[0],
    )
    w2_late = DeliveryWorker(
        session_factory=session_factory,
        email_sender=_FastEmailSenderWorker2(),
        sms_sender=MagicMock(spec=SmsSenderProtocol),
        delivery_lease_seconds=10,
        clock=lambda: t0 + datetime.timedelta(seconds=11),
    )

    seq_stats: dict[str, int] = {}

    def _run_seq() -> None:
        seq_stats.update(w_seq.process_once(limit=20))

    t_seq = threading.Thread(target=_run_seq)
    t_seq.start()
    assert task4_started.wait(timeout=5.0)

    # На t0+11s (> стария общ lease от t0+10s!) задача #1004 има активен пресен lease до t0+19s
    w2_late_stats = w2_late.process_once(limit=20)
    assert w2_late_stats["claimed"] == 0
    assert w2_late_stats["sent"] == 0

    allow_task4_finish.set()
    t_seq.join(timeout=5.0)
    assert not t_seq.is_alive()
    assert seq_stats["sent"] == 2
    assert seq_sent_orders == [1003, 1004]

    with session_factory() as s:
        deliveries = (
            s.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_id.in_([ev1, ev2, ev3, ev4]),
                    NotificationDelivery.channel == "email",
                )
            )
            .scalars()
            .all()
        )
        assert len(deliveries) == 4
        assert all(d.status == "sent" and d.attempt_count == 1 for d in deliveries)


def test_delivery_worker_and_settings_align_provider_timeout_and_lease(
    session_factory: sessionmaker,
) -> None:
    """Срокът на захвата (lease) трябва да е съгласуван (поне 3x) спрямо таймаута на провайдъра."""
    with pytest.raises(ValueError, match="DELIVERY_LEASE_SECONDS"):
        Settings(smtp_timeout_seconds=10.0, delivery_lease_seconds=20)

    with pytest.raises(ValueError, match="delivery_lease_seconds"):
        DeliveryWorker(
            session_factory=session_factory,
            email_sender=EmailSender(timeout=5.0),
            sms_sender=MagicMock(spec=SmsSenderProtocol),
            delivery_lease_seconds=10,
        )
