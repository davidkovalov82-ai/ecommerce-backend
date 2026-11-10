"""Процесор за публикуване на чакащи outbox събития на Payments към RabbitMQ."""

import datetime
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.publisher import EventPublisher, RabbitMQEventPublisher
from app.repository import PaymentRepository

logger = logging.getLogger(__name__)


class OutboxProcessor:
    """Публикува чакащите payment_outbox_events без задържане на DB заключване при I/O."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        publisher: EventPublisher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher or RabbitMQEventPublisher()

    def close(self) -> None:
        self._publisher.close()

    def process_pending_events(self, limit: int = 50) -> dict[str, int]:
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
            repo = PaymentRepository(session)
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
                        ev.payment_attempt_id,
                        dict(ev.payload),
                    )
                )

        stats = {"processed": len(events_data), "published": 0, "failed": 0}

        for (
            outbox_id,
            event_id,
            event_type,
            event_version,
            occurred_at,
            order_id,
            payment_attempt_id,
            payload,
        ) in events_data:
            envelope = {
                "event_id": event_id,
                "event_type": event_type,
                "event_version": event_version,
                "occurred_at": occurred_at.isoformat(),
                "producer": "payments",
                "data": payload,
            }

            try:
                self._publisher.publish_event(
                    routing_key=event_type,
                    envelope=envelope,
                )
            except Exception as exc:  # noqa: BLE001
                err_msg = f"{type(exc).__name__}: {exc}"[:1000]
                logger.warning(
                    "Failed to publish payment outbox event event_id=%s event_type=%s order_id=%s payment_attempt_id=%s error=%s",
                    event_id,
                    event_type,
                    order_id,
                    payment_attempt_id,
                    err_msg,
                )
                with self._session_factory() as session:
                    repo = PaymentRepository(session)
                    event = repo.get_outbox_event_by_id_for_update(outbox_id)
                    if event and event.status == "pending":
                        event.retry_count += 1
                        event.last_error = err_msg
                        session.commit()
                stats["failed"] += 1
                continue

            now = datetime.datetime.now(datetime.UTC)
            with self._session_factory() as session:
                repo = PaymentRepository(session)
                event = repo.get_outbox_event_by_id_for_update(outbox_id)
                if event and event.status == "pending":
                    event.status = "published"
                    event.published_at = now
                    event.processed_at = now
                    event.last_error = None
                    session.commit()
            logger.info(
                "Published payment outbox event event_id=%s event_type=%s order_id=%s payment_attempt_id=%s",
                event_id,
                event_type,
                order_id,
                payment_attempt_id,
            )
            stats["published"] += 1

        return stats
