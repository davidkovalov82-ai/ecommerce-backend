"""Процесор за обработка на чакащи outbox събития (Transactional Outbox Worker)."""

import datetime
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.modules.orders.mailer import EmailSender
from app.modules.orders.repository import OrderRepository

logger = logging.getLogger(__name__)


class OutboxProcessor:
    """Обработва чакащите outbox събития без задържане на DB заключване по време на SMTP."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        email_sender: EmailSender | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._email_sender = email_sender or EmailSender()

    def process_pending_events(self, limit: int = 50) -> dict[str, int]:
        """Извлича чакащи събития, изпраща имейли извън DB транзакцията и обновява статуса им.

        Връща статистика: {"processed": int, "sent": int, "failed": int}
        """
        # 1. Извличане на чакащите събития в кратка DB транзакция
        events_data: list[tuple[int, int, str, dict[str, Any]]] = []
        with self._session_factory() as session:
            repo = OrderRepository(session)
            pending = repo.list_pending_outbox_events(limit=limit)
            for ev in pending:
                events_data.append((ev.id, ev.order_id, ev.event_type, ev.payload))
            # Транзакцията приключва веднага тук, преди всякаква комуникация със SMTP!

        stats = {
            "processed": len(events_data),
            "sent": 0,
            "failed": 0,
        }

        # 2. Обработка на всяко събитие самостоятелно
        for event_id, order_id, event_type, payload in events_data:
            if event_type == "order.paid":
                success = self._process_order_paid_event(event_id, order_id, payload)
                if success:
                    stats["sent"] += 1
                else:
                    stats["failed"] += 1

        return stats

    def _process_order_paid_event(
        self, event_id: int, order_id: int, payload: dict[str, Any]
    ) -> bool:
        to_email = payload.get("customer_email") or ""
        total_minor = payload.get("total_minor") or 0
        currency = payload.get("currency") or "EUR"

        # Мрежова SMTP комуникация БЕЗ DB заключване
        try:
            self._email_sender.send_order_paid_email(
                to_email=to_email,
                order_id=order_id,
                total_minor=total_minor,
                currency=currency,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to send email for order #%s via SMTP: %s", order_id, exc
            )
            # При неуспех: отбелязваме опита в кратка DB транзакция, поръчката си остава paid!
            with self._session_factory() as session:
                repo = OrderRepository(session)
                event = repo.get_outbox_event_by_id_for_update(event_id)
                if event and event.status == "pending":
                    event.retry_count += 1
                    event.last_error = str(exc)[:1000]
                    session.commit()
            return False

        # При успех: отбелязваме събитието като 'sent' в кратка DB транзакция
        with self._session_factory() as session:
            repo = OrderRepository(session)
            event = repo.get_outbox_event_by_id_for_update(event_id)
            if event and event.status == "pending":
                event.status = "sent"
                event.processed_at = datetime.datetime.now(datetime.UTC)
                session.commit()
        return True
