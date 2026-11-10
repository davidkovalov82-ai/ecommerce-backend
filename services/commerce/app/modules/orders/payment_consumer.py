"""RabbitMQ consumer в Commerce за входящи платежни събития от Payments."""

import contextlib
import logging
import threading
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
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.payment_contracts import (
    PaymentContractError,
    ValidatedPaymentEvent,
    parse_and_validate_payment_event,
)
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService

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

BeforeAckHook = Callable[[ValidatedPaymentEvent, Any, Any], None]


class RabbitMQPaymentEventConsumer:
    """Консумира payment.* събития от RabbitMQ и прилага промяната към заказа с commit преди basic_ack."""

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
        before_ack_hook: BeforeAckHook | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.host = host or settings.rabbitmq_host
        self.port = port or settings.rabbitmq_port
        self.user = user or settings.rabbitmq_user
        self.password = password or settings.rabbitmq_password
        self.vhost = vhost or settings.rabbitmq_vhost
        self.exchange = exchange or settings.rabbitmq_payments_exchange
        self.events_queue = events_queue or settings.rabbitmq_payments_queue
        self.dlx_exchange = dlx_exchange or settings.rabbitmq_payments_dlx_exchange
        self.dlq_queue = dlq_queue or settings.rabbitmq_payments_dlq_queue
        self.dlq_routing_key = (
            dlq_routing_key or settings.rabbitmq_payments_dlq_routing_key
        )
        self.prefetch_count = (
            prefetch_count
            if prefetch_count is not None
            else settings.rabbitmq_prefetch_count
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
        """Декларира устойчивата топология (payments.events -> commerce.payment_events + DLX/DLQ)."""
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
        for routing_key in (
            "payment.succeeded",
            "payment.processing",
            "payment.failed",
            "payment.expired",
            "payment.cancelled",
        ):
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
        try:
            event = parse_and_validate_payment_event(body)
        except PaymentContractError as exc:
            logger.warning(
                "Dead-lettering invalid payment event delivery_tag=%s reason=%s",
                getattr(method, "delivery_tag", None),
                exc.reason,
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            with self._session_factory() as session:
                service = OrderService(
                    order_repository=OrderRepository(session),
                    cart_repository=CartRepository(session),
                    product_repository=ProductRepository(session),
                    session=session,
                )
                outcome = service.apply_payment_event(event)
        except DataError as exc:
            logger.warning(
                "Dead-lettering payment event DataError event_id=%s event_type=%s order_id=%s error=%s",
                event.event_id,
                event.event_type,
                event.order_id,
                type(exc).__name__,
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        except _TRANSIENT_DB_ERRORS as exc:
            logger.warning(
                "Temporary DB failure consuming payment event_id=%s event_type=%s order_id=%s error=%s",
                event.event_id,
                event.event_type,
                event.order_id,
                f"{type(exc).__name__}: {exc}"[:300],
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return

        logger.info(
            "Applied payment event event_id=%s event_type=%s order_id=%s payment_attempt_id=%s outcome=%s",
            event.event_id,
            event.event_type,
            event.order_id,
            event.payment_attempt_id,
            outcome,
        )

        if self._before_ack_hook is not None:
            self._before_ack_hook(event, channel, method)

        channel.basic_ack(delivery_tag=method.delivery_tag)

    def consume_available(
        self,
        *,
        max_messages: int = 100,
        idle_timeout_seconds: float = 0.3,
    ) -> int:
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
                    "RabbitMQ payment consumer connection lost error=%s; retrying in %.1fs",
                    f"{type(exc).__name__}: {exc}"[:300],
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
