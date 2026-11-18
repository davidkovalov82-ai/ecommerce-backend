"""RabbitMQ consumer в Commerce за входящи събития за статус на потребители от Users."""

import contextlib
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

import pika
import pika.exceptions
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.catalog.repository import SellerStatusRepository

logger = logging.getLogger(__name__)


class RabbitMQUserEventConsumer:
    """Консумира user.* събития от RabbitMQ и обновява локалната видимост на продавачите."""

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
        prefetch_count: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.host = host or settings.rabbitmq_host
        self.port = port or settings.rabbitmq_port
        self.user = user or settings.rabbitmq_user
        self.password = password or settings.rabbitmq_password
        self.vhost = vhost or settings.rabbitmq_vhost
        self.exchange = exchange or settings.rabbitmq_users_exchange
        self.events_queue = events_queue or settings.rabbitmq_users_queue
        self.prefetch_count = (
            prefetch_count
            if prefetch_count is not None
            else settings.rabbitmq_prefetch_count
        )

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
        """Декларира устойчивата топология (users.events -> commerce.user_events)."""
        channel.exchange_declare(
            exchange=self.exchange,
            exchange_type="topic",
            durable=True,
        )
        channel.queue_declare(
            queue=self.events_queue,
            durable=True,
        )
        channel.queue_bind(
            queue=self.events_queue,
            exchange=self.exchange,
            routing_key="user.*",
        )

    def _handle_message(
        self, channel: Any, method: Any, properties: Any, body: bytes
    ) -> None:
        """Обработва събитие за потребител идемпотентно със запис в seller_inbox_events."""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Unparseable user event body: %s; dead-lettering", exc)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        event_id = payload.get("event_id")
        event_type = payload.get("event_type", "user.status_changed")
        user_id = payload.get("user_id")
        status = payload.get("status")

        if not event_id or user_id is None or not status:
            logger.warning("Malformed user event payload: %s; dropping", payload)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        with self._session_factory() as session:
            repo = SellerStatusRepository(session)
            if repo.is_inbox_event_processed(str(event_id)):
                logger.info(
                    "User event %s already processed; acknowledging redelivery",
                    event_id,
                )
                channel.basic_ack(delivery_tag=method.delivery_tag)
                return

            repo.upsert_status(seller_id=int(user_id), status=str(status))
            repo.record_inbox_event_idempotent(
                event_id=str(event_id),
                seller_id=int(user_id),
                event_type=str(event_type),
                status=str(status),
            )
            session.commit()

        channel.basic_ack(delivery_tag=method.delivery_tag)

    def consume_available(self, max_messages: int = 100) -> int:
        """Извлича наличните събития еднократно (за тестове)."""
        connection: pika.BlockingConnection | None = None
        channel: Any = None
        processed = 0

        try:
            connection = pika.BlockingConnection(self._build_connection_parameters())
            channel = connection.channel()
            self.ensure_topology(channel)

            for _ in range(max_messages):
                method, properties, body = channel.basic_get(
                    queue=self.events_queue, auto_ack=False
                )
                if method is None:
                    break
                self._handle_message(channel, method, properties, body)
                processed += 1
        finally:
            if channel is not None:
                with contextlib.suppress(Exception):
                    if channel.is_open:
                        channel.close()
            if connection is not None:
                with contextlib.suppress(Exception):
                    if connection.is_open:
                        connection.close()

        return processed

    def run(
        self,
        stop_event: threading.Event | None = None,
        *,
        reconnect_delay_seconds: float = 1.0,
    ) -> None:
        """Постоянен цикъл за консумация на събития с автоматично повторно свързване."""
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
                    "RabbitMQ user consumer connection lost: %s; retrying in %.1fs",
                    exc,
                    reconnect_delay_seconds,
                )
                if stop_event is not None:
                    stop_event.wait(reconnect_delay_seconds)
                else:
                    threading.Event().wait(reconnect_delay_seconds)
            finally:
                if channel is not None:
                    with contextlib.suppress(Exception):
                        if channel.is_open:
                            channel.close()
                if connection is not None:
                    with contextlib.suppress(Exception):
                        if connection.is_open:
                            connection.close()
