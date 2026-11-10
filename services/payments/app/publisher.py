"""RabbitMQ издател за изходящите платежни събития от Payments към Commerce."""

import contextlib
import datetime
import json
from typing import Any, Protocol

import pika
import pika.exceptions

from app.config import settings


class EventPublishError(Exception):
    """Базова грешка при публикуване на платежно събитие към брокера."""


class BrokerUnavailableError(EventPublishError):
    """RabbitMQ брокерът е недостъпен или връзката прекъсна."""


class UnroutableEventError(EventPublishError):
    """Събитието е върнато от брокера поради липсващ маршрут (NO_ROUTE)."""


class PublishConfirmLostError(EventPublishError):
    """Брокерът не потвърди записа на събитието (Nack / timeout)."""


class EventPublisher(Protocol):
    """Протокол за издател на платежни събития."""

    def publish_event(self, *, routing_key: str, envelope: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class RabbitMQEventPublisher:
    """Надежден RabbitMQ издател с durable топология, delivery_mode=2, mandatory и publisher confirms."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        vhost: str | None = None,
        exchange: str | None = None,
        commerce_queue: str | None = None,
        dlx_exchange: str | None = None,
        dlq_queue: str | None = None,
        dlq_routing_key: str | None = None,
        socket_timeout: float | None = None,
        auto_declare_topology: bool = True,
    ) -> None:
        self.host = host or settings.rabbitmq_host
        self.port = port or settings.rabbitmq_port
        self.user = user or settings.rabbitmq_user
        self.password = password or settings.rabbitmq_password
        self.vhost = vhost or settings.rabbitmq_vhost
        self.exchange = exchange or settings.rabbitmq_exchange
        self.commerce_queue = commerce_queue or settings.rabbitmq_commerce_queue
        self.dlx_exchange = dlx_exchange or settings.rabbitmq_dlx_exchange
        self.dlq_queue = dlq_queue or settings.rabbitmq_dlq_queue
        self.dlq_routing_key = dlq_routing_key or settings.rabbitmq_dlq_routing_key
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
        """Декларира устойчивата топология за платежните събития към Commerce."""
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
            queue=self.commerce_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": self.dlx_exchange,
                "x-dead-letter-routing-key": self.dlq_routing_key,
            },
        )
        for routing_key in (
            "payment.succeeded",
            "payment.processing",
            "payment.failed",
            "payment.expired",
            "payment.cancelled",
        ):
            channel.queue_bind(
                queue=self.commerce_queue,
                exchange=self.exchange,
                routing_key=routing_key,
            )
        self._topology_declared = True

    def publish_event(self, *, routing_key: str, envelope: dict[str, Any]) -> None:
        """Публикува събитие с delivery_mode=2, mandatory=True и publisher confirm."""
        body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        event_id = str(envelope["event_id"])
        event_type = str(envelope["event_type"])
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
                mandatory=True,
            )
        except pika.exceptions.UnroutableError as exc:
            raise UnroutableEventError(
                f"Event {event_id} ({event_type}) returned unroutable for routing_key='{routing_key}'"
            ) from exc
        except pika.exceptions.NackError as exc:
            raise PublishConfirmLostError(
                f"Broker nacked event {event_id} ({event_type})"
            ) from exc
        except (
            pika.exceptions.AMQPError,
            AssertionError,
            OSError,
            TimeoutError,
        ) as exc:
            self.close()
            raise BrokerUnavailableError(
                f"{type(exc).__name__}: failed to publish event {event_id}"
            ) from exc

    def close(self) -> None:
        self._topology_declared = False
        if self._channel is not None:
            with contextlib.suppress(pika.exceptions.AMQPError, OSError):
                if self._channel.is_open:
                    self._channel.close()
            self._channel = None
        if self._connection is not None:
            with contextlib.suppress(pika.exceptions.AMQPError, OSError):
                if self._connection.is_open:
                    self._connection.close()
            self._connection = None
