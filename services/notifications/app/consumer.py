"""RabbitMQ consumer за Notifications: валидация, транзакционен inbox запис, commit преди ack и DLQ."""

import contextlib
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

import pika
import pika.exceptions
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from app.config import settings
from app.contracts import EventContractError, ValidatedEvent, parse_and_validate_event
from app.repository import NotificationRepository

logger = logging.getLogger(__name__)

_EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PHONE_PATTERN = re.compile(r"\+?\d[\d\s\-()]{5,}\d")

BeforeAckHook = Callable[[ValidatedEvent, Any, Any], None]


def _sanitize_error(exc: Exception) -> str:
    """Премахва потенциални имейли и телефони от текст на грешка преди запис в лог."""
    msg = f"{type(exc).__name__}: {exc}"
    msg = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", msg)
    msg = _PHONE_PATTERN.sub("[REDACTED_PHONE]", msg)
    return msg[:500]


class RabbitMQEventConsumer:
    """Консуматор на събития от RabbitMQ за независимия сервис Notifications."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        vhost: str | None = None,
        exchange: str | None = None,
        events_queue: str | None = None,
        dlx_exchange: str | None = None,
        dlq_queue: str | None = None,
        dlq_routing_key: str | None = None,
        prefetch_count: int | None = None,
        email_max_attempts: int | None = None,
        sms_max_attempts: int | None = None,
        before_ack_hook: BeforeAckHook | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.host = host or settings.rabbitmq_host
        self.port = port or settings.rabbitmq_port
        self.user = user or settings.rabbitmq_user
        self.password = password or settings.rabbitmq_password
        self.vhost = vhost or settings.rabbitmq_vhost
        self.exchange = exchange or settings.rabbitmq_exchange
        self.events_queue = events_queue or settings.rabbitmq_events_queue
        self.dlx_exchange = dlx_exchange or settings.rabbitmq_dlx_exchange
        self.dlq_queue = dlq_queue or settings.rabbitmq_dlq_queue
        self.dlq_routing_key = dlq_routing_key or settings.rabbitmq_dlq_routing_key
        self.prefetch_count = (
            prefetch_count
            if prefetch_count is not None
            else settings.rabbitmq_prefetch_count
        )
        self.email_max_attempts = (
            email_max_attempts
            if email_max_attempts is not None
            else settings.email_max_attempts
        )
        self.sms_max_attempts = (
            sms_max_attempts
            if sms_max_attempts is not None
            else settings.sms_max_attempts
        )
        self._before_ack_hook = before_ack_hook

    def _build_connection_parameters(self) -> pika.ConnectionParameters:
        credentials = pika.PlainCredentials(self.user, self.password)
        return pika.ConnectionParameters(
            host=self.host,
            port=self.port,
            virtual_host=self.vhost,
            credentials=credentials,
            heartbeat=30,
            blocked_connection_timeout=5.0,
            socket_timeout=5.0,
            connection_attempts=1,
        )

    def ensure_topology(self, channel: Any) -> None:
        """Декларира устойчивата топология (exchange, queue, bindings и DLX/DLQ)."""
        channel.exchange_declare(
            exchange=self.exchange,
            exchange_type="topic",
            durable=True,
        )
        channel.exchange_declare(
            exchange=self.dlx_exchange,
            exchange_type="direct",
            durable=True,
        )
        channel.queue_declare(queue=self.dlq_queue, durable=True)
        channel.queue_bind(
            queue=self.dlq_queue,
            exchange=self.dlx_exchange,
            routing_key=self.dlq_routing_key,
        )
        channel.queue_declare(
            queue=self.events_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": self.dlx_exchange,
                "x-dead-letter-routing-key": self.dlq_routing_key,
            },
        )
        for routing_key in ("order.paid", "shipment.shipped"):
            channel.queue_bind(
                queue=self.events_queue,
                exchange=self.exchange,
                routing_key=routing_key,
            )

    def _handle_message(
        self,
        channel: Any,
        method: Any,
        _properties: Any,
        body: bytes,
    ) -> None:
        """Обработва единично AMQP съобщение: валидира -> DB commit -> basic_ack."""
        # 1. Стриктна валидация на контракта и версията; невалидните -> DLQ без повторение
        # Важно: не извеждаме в лога сурови event_id, event_type или event_version от невалидно съобщение
        try:
            event = parse_and_validate_event(body)
        except EventContractError as exc:
            logger.warning(
                "Dead-lettering invalid event delivery_tag=%s reason=%s",
                getattr(method, "delivery_tag", None),
                exc.reason,
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        # 2. Запис в една PostgreSQL транзакция на inbox_events и notification_deliveries
        try:
            with self._session_factory() as session:
                repo = NotificationRepository(session)
                ingest_result = repo.ingest_event(
                    event,
                    email_max_attempts=self.email_max_attempts,
                    sms_max_attempts=self.sms_max_attempts,
                )
                session.commit()
        except DataError as exc:
            logger.warning(
                "Dead-lettering DB DataError event_id=%s event_type=%s order_id=%s shipment_id=%s error=%s",
                event.event_id,
                event.event_type,
                event.order_id,
                event.shipment_id,
                _sanitize_error(exc),
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Temporary DB failure while ingesting event_id=%s event_type=%s order_id=%s shipment_id=%s error=%s",
                event.event_id,
                event.event_type,
                event.order_id,
                event.shipment_id,
                _sanitize_error(exc),
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return

        logger.info(
            "Ingested event event_id=%s event_type=%s order_id=%s shipment_id=%s redelivered=%s inserted=%s created_channels=%s",
            event.event_id,
            event.event_type,
            event.order_id,
            event.shipment_id,
            bool(getattr(method, "redelivered", False)),
            ingest_result.event_inserted,
            ",".join(ingest_result.created_channels) or "none",
        )
        for skipped_channel, skip_reason in ingest_result.skipped_channels.items():
            logger.info(
                "Skipped channel event_id=%s order_id=%s shipment_id=%s channel=%s reason=%s",
                event.event_id,
                event.order_id,
                event.shipment_id,
                skipped_channel,
                skip_reason,
            )

        # 3. Ack в RabbitMQ се изпраща САМО след успешен PostgreSQL commit
        if self._before_ack_hook is not None:
            self._before_ack_hook(event, channel, method)

        channel.basic_ack(delivery_tag=method.delivery_tag)

    def consume_available(
        self,
        *,
        max_messages: int = 100,
        idle_timeout_seconds: float = 0.3,
    ) -> int:
        """Консумира наличните съобщения в опашката и приключва (удобно за тестове и batch)."""
        processed = 0
        connection: pika.BlockingConnection | None = None
        channel: Any = None
        try:
            connection = pika.BlockingConnection(self._build_connection_parameters())
            channel = connection.channel()
            self.ensure_topology(channel)
            channel.basic_qos(prefetch_count=self.prefetch_count)

            for method, properties, body in channel.consume(
                queue=self.events_queue,
                auto_ack=False,
                inactivity_timeout=idle_timeout_seconds,
            ):
                if method is None:
                    break
                self._handle_message(channel, method, properties, body)
                processed += 1
                if processed >= max_messages:
                    break

            if channel.is_open:
                channel.cancel()
        finally:
            if channel is not None:
                with contextlib.suppress(
                    pika.exceptions.AMQPError, OSError, AssertionError
                ):
                    if channel.is_open:
                        channel.close()
            if connection is not None:
                with contextlib.suppress(
                    pika.exceptions.AMQPError, OSError, AssertionError
                ):
                    if connection.is_open:
                        connection.close()
        return processed

    def run(
        self,
        stop_event: threading.Event | None = None,
        *,
        reconnect_delay_seconds: float = 1.0,
    ) -> None:
        """Непрекъснат цикъл на консумиране с автоматично пресвързване при срив на брокера."""
        while stop_event is None or not stop_event.is_set():
            connection: pika.BlockingConnection | None = None
            channel: Any = None
            try:
                connection = pika.BlockingConnection(
                    self._build_connection_parameters()
                )
                channel = connection.channel()
                self.ensure_topology(channel)
                channel.basic_qos(prefetch_count=self.prefetch_count)
                logger.info(
                    "Connected to RabbitMQ queue=%s exchange=%s",
                    self.events_queue,
                    self.exchange,
                )

                for method, properties, body in channel.consume(
                    queue=self.events_queue,
                    auto_ack=False,
                    inactivity_timeout=0.5,
                ):
                    if stop_event is not None and stop_event.is_set():
                        break
                    if method is None:
                        continue
                    self._handle_message(channel, method, properties, body)

                if channel is not None and channel.is_open:
                    channel.cancel()
            except (pika.exceptions.AMQPError, OSError, TimeoutError) as exc:
                if stop_event is not None and stop_event.is_set():
                    break
                logger.warning(
                    "RabbitMQ consumer connection lost error=%s; retrying in %.1fs",
                    _sanitize_error(exc),
                    reconnect_delay_seconds,
                )
                if stop_event is not None:
                    stop_event.wait(reconnect_delay_seconds)
                else:
                    threading.Event().wait(reconnect_delay_seconds)
            finally:
                if channel is not None:
                    with contextlib.suppress(
                        pika.exceptions.AMQPError, OSError, AssertionError
                    ):
                        if channel.is_open:
                            channel.close()
                if connection is not None:
                    with contextlib.suppress(
                        pika.exceptions.AMQPError, OSError, AssertionError
                    ):
                        if connection.is_open:
                            connection.close()
