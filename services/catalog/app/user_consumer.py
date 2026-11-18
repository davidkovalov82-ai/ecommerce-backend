"""RabbitMQ consumer в Catalog за входящи събития за статус на потребители от Users."""

import contextlib
import datetime
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import pika
import pika.exceptions
import psycopg
from sqlalchemy.exc import (
    DataError,
    DisconnectionError,
    InterfaceError,
    OperationalError,
)
from sqlalchemy.exc import (
    TimeoutError as SQLAlchemyTimeoutError,
)
from sqlalchemy.orm import Session

from app.config import settings
from app.repository import SellerStatusRepository

logger = logging.getLogger(__name__)

_TRANSIENT_DB_ERRORS = (
    OperationalError,
    InterfaceError,
    DisconnectionError,
    SQLAlchemyTimeoutError,
    psycopg.OperationalError,
    psycopg.InterfaceError,
    OSError,
    TimeoutError,
)


class RabbitMQUserEventConsumer:
    """Консумира user.* събития от RabbitMQ и обновява видимостта на продавачите в Catalog."""

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
        retry_delay: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self.retry_delay = retry_delay
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
        """Декларира топология (users.events -> catalog.user_events)."""
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
        """Обработва събитие за потребител идемпотентно и защитено от стари събития."""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Unparseable user event body: %s; dead-lettering", exc)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        if not isinstance(payload, dict):
            logger.warning(
                "User event payload is not a JSON object: %s; dropping", payload
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        event_id = payload.get("event_id")
        event_type = payload.get("event_type", "user.status_changed")
        user_id = payload.get("user_id")
        status = payload.get("status")
        occurred_at_raw = payload.get("occurred_at")

        if not event_id or not isinstance(event_id, str):
            logger.warning(
                "Missing or invalid event_id in payload: %s; dropping", payload
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            seller_id = int(user_id)  # type: ignore[arg-type]
            if seller_id <= 0:
                raise ValueError("user_id must be positive")
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Invalid user_id in user event: %s (%s); dropping", user_id, exc
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        if (
            not status
            or not isinstance(status, str)
            or status not in ("active", "blocked", "closed")
        ):
            logger.warning("Invalid status in user event: %s; dropping", status)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        if not occurred_at_raw:
            logger.warning(
                "Missing occurred_at in user event payload: %s; dead-lettering", payload
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            event_time = datetime.datetime.fromisoformat(str(occurred_at_raw))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=datetime.UTC)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Invalid occurred_at in user event: %s (%s); dead-lettering",
                occurred_at_raw,
                exc,
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            with self._session_factory() as session:
                repo = SellerStatusRepository(session)
                # 1. Атомарна дедупликация чрез опит за запис в inbox
                is_new = repo.record_inbox_event_idempotent(
                    event_id=str(event_id),
                    seller_id=seller_id,
                    event_type=str(event_type),
                    status=str(status),
                )
                if not is_new:
                    logger.info(
                        "User event %s already processed; acknowledging redelivery",
                        event_id,
                    )
                    session.commit()
                    channel.basic_ack(delivery_tag=method.delivery_tag)
                    return

                # 2. Обновяване на статус със защита от по-стари събития
                repo.upsert_status(
                    seller_id=seller_id,
                    status=str(status),
                    updated_at=event_time,
                )
                session.commit()

            channel.basic_ack(delivery_tag=method.delivery_tag)
        except DataError as exc:
            logger.warning(
                "Dead-lettering user event DataError event_id=%s seller_id=%s error=%s",
                event_id,
                seller_id,
                exc,
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        except _TRANSIENT_DB_ERRORS as exc:
            logger.warning(
                "Temporary DB failure processing user event %s: %s; requeuing with delay",
                event_id,
                exc,
            )
            if self.retry_delay > 0:
                time.sleep(self.retry_delay)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Unexpected non-retryable error processing user event %s: %s; dropping",
                event_id,
                exc,
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

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
            except (
                pika.exceptions.AMQPConnectionError,
                pika.exceptions.AMQPChannelError,
            ) as exc:
                logger.warning(
                    "RabbitMQ connection lost (%s); reconnecting in %.1fs...",
                    exc,
                    reconnect_delay_seconds,
                )
                if stop_event is not None:
                    stop_event.wait(timeout=reconnect_delay_seconds)
            finally:
                if channel is not None:
                    with contextlib.suppress(Exception):
                        if channel.is_open:
                            channel.close()
                if connection is not None:
                    with contextlib.suppress(Exception):
                        if connection.is_open:
                            connection.close()
