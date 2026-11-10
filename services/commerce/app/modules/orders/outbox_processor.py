"""Процесор за публикуване на чакащи outbox събития към RabbitMQ (Transactional Outbox)."""

import datetime
import logging
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.modules.orders.publisher import EventPublisher, RabbitMQEventPublisher
from app.modules.orders.repository import OrderRepository

logger = logging.getLogger(__name__)

_EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _sanitize_error_message(exc: Exception, payload: dict[str, Any]) -> str:
    """Премахва клиентски контакти (имейл/телефон) от текста на грешката преди запис или лог."""
    message = f"{type(exc).__name__}: {exc}"
    for key in ("customer_email", "customer_phone"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            message = message.replace(val.strip(), "[REDACTED]")
    message = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", message)
    return message[:1000]


class OutboxProcessor:
    """Чете чакащите outbox събития и ги публикува в RabbitMQ без DB заключване по време на I/O.

    Отбелязва събитието като 'published' едва след потвърждение от брокера и проверен маршрут.
    При грешка оставя записа в 'pending' със същия стабилен event_id за повторен опит.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        publisher: EventPublisher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher or RabbitMQEventPublisher()

    def close(self) -> None:
        """Затваря връзката на издателя при спиране на процеса."""
        self._publisher.close()

    def process_pending_events(self, limit: int = 50) -> dict[str, int]:
        """Публикува чакащите събития в RabbitMQ и обновява статуса в отделна кратка транзакция.

        Връща статистика: {"processed": int, "published": int, "failed": int}
        """
        events_data: list[
            tuple[
                int,
                str,
                str,
                int,
                datetime.datetime,
                int,
                int | None,
                dict[str, Any],
            ]
        ] = []

        with self._session_factory() as session:
            repo = OrderRepository(session)
            pending = repo.list_pending_outbox_events(limit=limit)
            for ev in pending:
                events_data.append(
                    (
                        ev.id,
                        ev.event_id,
                        ev.event_type,
                        ev.event_version,
                        ev.occurred_at,
                        ev.order_id,
                        ev.shipment_id,
                        dict(ev.payload),
                    )
                )

        stats = {
            "processed": len(events_data),
            "published": 0,
            "failed": 0,
        }

        for (
            outbox_id,
            event_id,
            event_type,
            event_version,
            occurred_at,
            order_id,
            shipment_id,
            payload,
        ) in events_data:
            envelope = {
                "event_id": event_id,
                "event_type": event_type,
                "event_version": event_version,
                "occurred_at": occurred_at.isoformat(),
                "producer": "commerce",
                "data": payload,
            }

            try:
                self._publisher.publish_event(
                    routing_key=event_type,
                    envelope=envelope,
                )
            except Exception as exc:  # noqa: BLE001
                safe_error = _sanitize_error_message(exc, payload)
                logger.warning(
                    "Failed to publish outbox event event_id=%s event_type=%s order_id=%s shipment_id=%s error=%s",
                    event_id,
                    event_type,
                    order_id,
                    shipment_id,
                    safe_error,
                )
                with self._session_factory() as session:
                    repo = OrderRepository(session)
                    event = repo.get_outbox_event_by_id_for_update(outbox_id)
                    if event and event.status == "pending":
                        event.retry_count += 1
                        event.last_error = safe_error
                        session.commit()
                stats["failed"] += 1
                continue

            now = datetime.datetime.now(datetime.UTC)
            with self._session_factory() as session:
                repo = OrderRepository(session)
                event = repo.get_outbox_event_by_id_for_update(outbox_id)
                if event and event.status == "pending":
                    event.status = "published"
                    event.published_at = now
                    event.processed_at = now
                    event.last_error = None
                    session.commit()
            logger.info(
                "Published outbox event event_id=%s event_type=%s order_id=%s shipment_id=%s",
                event_id,
                event_type,
                order_id,
                shipment_id,
            )
            stats["published"] += 1

        return stats
