"""RabbitMQ издател на домейнови събития от Users (промяна на статус на потребители)."""

import contextlib
import datetime
import json
import logging
from typing import Any, Protocol

import pika
import pika.exceptions

from app.config import settings

logger = logging.getLogger(__name__)


class EventPublishError(Exception):
    """Базова грешка при публикуване на събитие към брокера."""


class BrokerUnavailableError(EventPublishError):
    """RabbitMQ брокерът е недостъпен."""


class EventPublisher(Protocol):
    """Протокол за издател на събития към брокера."""

    def publish_event(self, *, routing_key: str, envelope: dict[str, Any]) -> None:
        """Публикува събитие към брокера."""
        ...

    def close(self) -> None:
        """Затваря ресурсите."""
        ...


class RabbitMQUserEventPublisher:
    """RabbitMQ издател с durable topic exchange users.events и publisher confirms."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        vhost: str | None = None,
        exchange: str | None = None,
        socket_timeout: float | None = None,
        auto_declare_topology: bool = True,
    ) -> None:
        self.host = host or settings.rabbitmq_host
        self.port = port or settings.rabbitmq_port
        self.user = user or settings.rabbitmq_user
        self.password = password or settings.rabbitmq_password
        self.vhost = vhost or settings.rabbitmq_vhost
        self.exchange = exchange or settings.rabbitmq_exchange
        self.socket_timeout = (
            socket_timeout
            if socket_timeout is not None
            else settings.rabbitmq_socket_timeout_seconds
        )
        self.auto_declare_topology = auto_declare_topology

        self._connection: pika.BlockingConnection | None = None
        self._channel: Any = None
        self._topology_declared = False

    def _build_connection_parameters(self) -> pika.ConnectionParameters:
        credentials = pika.PlainCredentials(self.user, self.password)
        return pika.ConnectionParameters(
            host=self.host,
            port=self.port,
            virtual_host=self.vhost,
            credentials=credentials,
            heartbeat=30,
            blocked_connection_timeout=self.socket_timeout,
            socket_timeout=self.socket_timeout,
            connection_attempts=1,
        )

    def _ensure_channel(self) -> Any:
        if (
            self._connection is not None
            and self._connection.is_open
            and self._channel is not None
            and self._channel.is_open
        ):
            return self._channel

        self.close()
        connection = pika.BlockingConnection(self._build_connection_parameters())
        channel = connection.channel()
        channel.confirm_delivery()
        self._connection = connection
        self._channel = channel
        self._topology_declared = False
        return channel

    def ensure_topology(self) -> None:
        """Декларира устойчивата топология за събитията на Users."""
        try:
            channel = self._ensure_channel()
            self._declare_topology(channel)
        except (
            pika.exceptions.AMQPError,
            OSError,
            TimeoutError,
            AssertionError,
        ) as exc:
            self.close()
            raise BrokerUnavailableError(
                f"{type(exc).__name__}: failed to declare RabbitMQ topology"
            ) from exc

    def _declare_topology(self, channel: Any) -> None:
        channel.exchange_declare(
            exchange=self.exchange,
            exchange_type="topic",
            durable=True,
        )
        self._topology_declared = True

    def publish_event(self, *, routing_key: str, envelope: dict[str, Any]) -> None:
        """Публикува събитие и изчаква publisher confirm от RabbitMQ."""
        body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        event_id = str(envelope.get("event_id", ""))
        event_type = str(envelope.get("event_type", ""))
        properties = pika.BasicProperties(
            delivery_mode=pika.DeliveryMode.Persistent,
            content_type="application/json",
            message_id=event_id,
            type=event_type,
            timestamp=int(datetime.datetime.now(datetime.UTC).timestamp()),
        )

        try:
            channel = self._ensure_channel()
            if self.auto_declare_topology and not self._topology_declared:
                self._declare_topology(channel)

            channel.basic_publish(
                exchange=self.exchange,
                routing_key=routing_key,
                body=body,
                properties=properties,
                mandatory=False,
            )
        except (
            pika.exceptions.AMQPError,
            OSError,
            TimeoutError,
            AssertionError,
        ) as exc:
            self.close()
            raise BrokerUnavailableError(
                f"{type(exc).__name__}: connection failure while publishing {event_type}"
            ) from exc

    def close(self) -> None:
        """Затваря ресурсите."""
        if self._channel is not None:
            with contextlib.suppress(Exception):
                if self._channel.is_open:
                    self._channel.close()
            self._channel = None

        if self._connection is not None:
            with contextlib.suppress(Exception):
                if self._connection.is_open:
                    self._connection.close()
            self._connection = None

        self._topology_declared = False
