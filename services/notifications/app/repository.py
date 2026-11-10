"""Транзакционно съхранение на събития и канални задания в Notifications БД."""

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts import ValidatedEvent
from app.models import InboxEvent, NotificationDelivery


@dataclass(frozen=True)
class IngestResult:
    """Резултат от транзакционния запис на входящо събитие."""

    event_inserted: bool
    created_channels: tuple[str, ...]
    skipped_channels: dict[str, str]


@dataclass(frozen=True)
class ClaimedDelivery:
    """Снимка на заключено за обработка задание по конкретен канал."""

    delivery_id: int
    event_id: str
    channel: str
    attempt_count: int
    max_attempts: int
    processing_token: str
    reclaimed: bool
    event_type: str
    order_id: int | None
    shipment_id: int | None
    payload: dict[str, Any]


class NotificationRepository:
    """Операции с PostgreSQL таблиците на Notifications."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def ingest_event(
        self,
        event: ValidatedEvent,
        *,
        email_max_attempts: int,
        sms_max_attempts: int,
        now: datetime.datetime | None = None,
    ) -> IngestResult:
        """В една транзакция записва входящото събитие и каналните задания идемпотентно.

        Използва ON CONFLICT DO NOTHING върху PRIMARY KEY(event_id) и
        UNIQUE(event_id, channel), за да гарантира най-много едно задание на канал
        дори при едновременна повторна доставка от RabbitMQ.
        """
        current_time = now or datetime.datetime.now(datetime.UTC)

        inbox_stmt = (
            pg_insert(InboxEvent)
            .values(
                event_id=event.event_id,
                event_type=event.event_type,
                event_version=event.event_version,
                order_id=event.order_id,
                shipment_id=event.shipment_id,
                payload=event.payload,
                occurred_at=event.occurred_at,
                received_at=current_time,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(InboxEvent.event_id)
        )
        inserted_event_id = self._session.execute(inbox_stmt).scalar_one_or_none()

        skipped_channels: dict[str, str] = {}
        rows_to_insert: list[dict[str, Any]] = []

        if event.customer_email:
            rows_to_insert.append(
                {
                    "event_id": event.event_id,
                    "channel": "email",
                    "status": "pending",
                    "attempt_count": 0,
                    "max_attempts": email_max_attempts,
                    "next_retry_at": current_time,
                    "last_error": None,
                    "created_at": current_time,
                    "updated_at": current_time,
                }
            )
        else:
            skipped_channels["email"] = "missing_customer_email"
            rows_to_insert.append(
                {
                    "event_id": event.event_id,
                    "channel": "email",
                    "status": "skipped",
                    "attempt_count": 0,
                    "max_attempts": 1,
                    "next_retry_at": current_time,
                    "last_error": "missing_customer_email",
                    "created_at": current_time,
                    "updated_at": current_time,
                }
            )

        if event.customer_phone:
            rows_to_insert.append(
                {
                    "event_id": event.event_id,
                    "channel": "sms",
                    "status": "pending",
                    "attempt_count": 0,
                    "max_attempts": sms_max_attempts,
                    "next_retry_at": current_time,
                    "last_error": None,
                    "created_at": current_time,
                    "updated_at": current_time,
                }
            )
        else:
            skipped_channels["sms"] = "missing_customer_phone"
            rows_to_insert.append(
                {
                    "event_id": event.event_id,
                    "channel": "sms",
                    "status": "skipped",
                    "attempt_count": 0,
                    "max_attempts": 1,
                    "next_retry_at": current_time,
                    "last_error": "missing_customer_phone",
                    "created_at": current_time,
                    "updated_at": current_time,
                }
            )

        delivery_stmt = (
            pg_insert(NotificationDelivery)
            .values(rows_to_insert)
            .on_conflict_do_nothing(index_elements=["event_id", "channel"])
            .returning(NotificationDelivery.channel)
        )
        created_channels = tuple(self._session.execute(delivery_stmt).scalars().all())

        return IngestResult(
            event_inserted=inserted_event_id is not None,
            created_channels=created_channels,
            skipped_channels=skipped_channels,
        )

    def expire_exhausted_processing_leases(
        self, now: datetime.datetime | None = None
    ) -> int:
        """Маркира като 'failed' изоставени задачи с изтекъл lease и изчерпани опити."""
        current_time = now or datetime.datetime.now(datetime.UTC)
        stmt = (
            update(NotificationDelivery)
            .where(
                NotificationDelivery.status == "processing",
                NotificationDelivery.locked_until.is_not(None),
                NotificationDelivery.locked_until <= current_time,
                NotificationDelivery.attempt_count >= NotificationDelivery.max_attempts,
            )
            .values(
                status="failed",
                locked_until=None,
                processing_token=None,
                last_error="worker_lease_expired_max_attempts",
                updated_at=current_time,
            )
        )
        result = self._session.execute(stmt)
        return int(result.rowcount or 0)

    def claim_due_deliveries(
        self,
        *,
        limit: int = 20,
        lease_seconds: int = 60,
        now: datetime.datetime | None = None,
    ) -> list[ClaimedDelivery]:
        """Заключва готовите за обработка или изтекли (crash recovery) задачи по канали."""
        current_time = now or datetime.datetime.now(datetime.UTC)
        self.expire_exhausted_processing_leases(now=current_time)

        stmt = (
            select(NotificationDelivery)
            .where(
                or_(
                    and_(
                        NotificationDelivery.status == "pending",
                        NotificationDelivery.next_retry_at <= current_time,
                        NotificationDelivery.attempt_count
                        < NotificationDelivery.max_attempts,
                    ),
                    and_(
                        NotificationDelivery.status == "processing",
                        NotificationDelivery.locked_until.is_not(None),
                        NotificationDelivery.locked_until <= current_time,
                        NotificationDelivery.attempt_count
                        < NotificationDelivery.max_attempts,
                    ),
                )
            )
            .order_by(
                NotificationDelivery.next_retry_at.asc(),
                NotificationDelivery.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = self._session.scalars(stmt).all()
        if not rows:
            return []

        event_ids = sorted({row.event_id for row in rows})
        events = self._session.scalars(
            select(InboxEvent).where(InboxEvent.event_id.in_(event_ids))
        ).all()
        events_by_id = {ev.event_id: ev for ev in events}

        lease_deadline = current_time + datetime.timedelta(seconds=lease_seconds)
        claimed: list[ClaimedDelivery] = []
        for row in rows:
            ev = events_by_id.get(row.event_id)
            if ev is None:
                continue
            was_reclaimed = row.status == "processing"
            token = str(uuid.uuid4())
            row.status = "processing"
            row.attempt_count += 1
            row.locked_until = lease_deadline
            row.processing_token = token
            row.updated_at = current_time
            claimed.append(
                ClaimedDelivery(
                    delivery_id=row.id,
                    event_id=row.event_id,
                    channel=row.channel,
                    attempt_count=row.attempt_count,
                    max_attempts=row.max_attempts,
                    processing_token=token,
                    reclaimed=was_reclaimed,
                    event_type=ev.event_type,
                    order_id=ev.order_id,
                    shipment_id=ev.shipment_id,
                    payload=dict(ev.payload),
                )
            )
        return claimed

    def finalize_delivery_success(
        self,
        claimed: ClaimedDelivery,
        *,
        provider_message_id: str | None = None,
        now: datetime.datetime | None = None,
    ) -> bool:
        """Отбелязва доставката като 'sent', само ако fencing token-ът все още съвпада."""
        current_time = now or datetime.datetime.now(datetime.UTC)
        row = self._session.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.id == claimed.delivery_id)
            .with_for_update()
        ).first()
        if (
            row is None
            or row.status != "processing"
            or row.processing_token != claimed.processing_token
        ):
            return False

        row.status = "sent"
        row.sent_at = current_time
        row.provider_message_id = provider_message_id
        row.locked_until = None
        row.processing_token = None
        row.last_error = None
        row.updated_at = current_time
        return True

    def finalize_delivery_failure(
        self,
        claimed: ClaimedDelivery,
        *,
        error_message: str,
        retry_delay_seconds: int,
        now: datetime.datetime | None = None,
    ) -> str:
        """Планира повторен опит или финализира 'failed' според лимита на канала."""
        current_time = now or datetime.datetime.now(datetime.UTC)
        row = self._session.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.id == claimed.delivery_id)
            .with_for_update()
        ).first()
        if (
            row is None
            or row.status != "processing"
            or row.processing_token != claimed.processing_token
        ):
            return "stale"

        row.locked_until = None
        row.processing_token = None
        row.last_error = error_message[:500]
        row.updated_at = current_time

        if row.attempt_count < row.max_attempts:
            row.status = "pending"
            row.next_retry_at = current_time + datetime.timedelta(
                seconds=retry_delay_seconds
            )
            return "retry_scheduled"

        row.status = "failed"
        return "failed"
