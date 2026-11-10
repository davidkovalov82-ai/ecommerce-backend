"""Процесор за публикуване на чакащи outbox събития на Users към RabbitMQ."""

import datetime
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.publisher import EventPublisher, EventPublishError, RabbitMQUserEventPublisher
from app.repository import UserRepository

logger = logging.getLogger(__name__)


class OutboxProcessor:
    """Публикува чакащите user_outbox_events към RabbitMQ."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        publisher: EventPublisher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher or RabbitMQUserEventPublisher()

    def close(self) -> None:
        self._publisher.close()

    def process_pending_events(self, limit: int = 50) -> dict[str, int]:
        """Извлича чакащи събития, публикува ги към RabbitMQ и отбелязва published_at."""
        events_data: list[tuple[int, str, str, dict[str, Any]]] = []

        with self._session_factory() as session:
            repo = UserRepository(session)
            pending = repo.list_pending_outbox_events(limit=limit)
            for ev in pending:
                events_data.append(
                    (ev.id, ev.event_id, ev.event_type, dict(ev.payload))
                )

        if not events_data:
            return {"processed": 0, "failed": 0}

        processed = 0
        failed = 0

        for _db_id, event_id, event_type, payload in events_data:
            routing_key = (
                event_type  # e.g. "user.blocked", "user.unblocked", "user.closed"
            )
            envelope = {
                "event_id": event_id,
                "event_type": event_type,
                "user_id": payload.get("user_id"),
                "status": payload.get("status"),
                "action": payload.get("action"),
                "occurred_at": payload.get("occurred_at"),
            }
            try:
                self._publisher.publish_event(
                    routing_key=routing_key, envelope=envelope
                )
                with self._session_factory() as session:
                    repo = UserRepository(session)
                    repo.mark_outbox_event_published(
                        event_id=event_id,
                        published_at=datetime.datetime.now(datetime.UTC),
                    )
                    session.commit()
                processed += 1
            except EventPublishError as exc:
                logger.error(
                    "Failed to publish user outbox event %s (%s): %s",
                    event_id,
                    event_type,
                    exc,
                )
                failed += 1
                break

        return {"processed": processed, "failed": failed}
