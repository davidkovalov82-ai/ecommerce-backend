"""PostgreSQL заявки на Payments сервиса (без бизнес комити — транзакциите се управляват в service слоя)."""

import datetime
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import (
    PaymentAttempt,
    PaymentDiscrepancy,
    PaymentOrderClosure,
    PaymentOutboxEvent,
    StripeCheckoutSession,
    StripeProcessedEvent,
)


class PaymentRepository:
    """Достъп до таблиците на Payments БД."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def acquire_order_lock(self, order_id: int) -> None:
        """Кратко транзакционно advisory заключване по order_id (без таблица orders в Payments БД)."""
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": int(order_id)},
        )

    def add_attempt(self, attempt: PaymentAttempt) -> PaymentAttempt:
        self._session.add(attempt)
        return attempt

    def add_stripe_session(
        self, session_record: StripeCheckoutSession
    ) -> StripeCheckoutSession:
        self._session.add(session_record)
        return session_record

    def get_attempt_by_id_for_update(self, attempt_id: int) -> PaymentAttempt | None:
        stmt = (
            select(PaymentAttempt)
            .where(PaymentAttempt.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def get_attempt_by_order_and_key_for_update(
        self, order_id: int, attempt_key: str
    ) -> PaymentAttempt | None:
        stmt = (
            select(PaymentAttempt)
            .where(
                PaymentAttempt.order_id == order_id,
                PaymentAttempt.attempt_key == attempt_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def get_active_attempt_by_order_for_update(
        self, order_id: int
    ) -> PaymentAttempt | None:
        stmt = (
            select(PaymentAttempt)
            .where(
                PaymentAttempt.order_id == order_id,
                PaymentAttempt.status.in_(
                    ["initiated", "open", "processing", "cancelling"]
                ),
            )
            .order_by(PaymentAttempt.id.desc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def get_succeeded_attempt_by_order_for_update(
        self, order_id: int
    ) -> PaymentAttempt | None:
        stmt = (
            select(PaymentAttempt)
            .where(
                PaymentAttempt.order_id == order_id,
                PaymentAttempt.status == "succeeded",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def get_latest_attempt_by_order_for_update(
        self, order_id: int
    ) -> PaymentAttempt | None:
        stmt = (
            select(PaymentAttempt)
            .where(PaymentAttempt.order_id == order_id)
            .order_by(PaymentAttempt.id.desc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def get_stripe_session_by_id_for_update(
        self, session_db_id: int
    ) -> StripeCheckoutSession | None:
        stmt = (
            select(StripeCheckoutSession)
            .where(StripeCheckoutSession.id == session_db_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def get_stripe_session_by_attempt_id_for_update(
        self, payment_attempt_id: int
    ) -> StripeCheckoutSession | None:
        stmt = (
            select(StripeCheckoutSession)
            .where(StripeCheckoutSession.payment_attempt_id == payment_attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def get_stripe_session_by_stripe_id(
        self, stripe_session_id: str
    ) -> StripeCheckoutSession | None:
        stmt = select(StripeCheckoutSession).where(
            StripeCheckoutSession.stripe_session_id == stripe_session_id
        )
        return self._session.scalars(stmt).first()

    def get_stripe_session_by_stripe_id_for_update(
        self, stripe_session_id: str
    ) -> StripeCheckoutSession | None:
        stmt = (
            select(StripeCheckoutSession)
            .where(StripeCheckoutSession.stripe_session_id == stripe_session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def get_latest_stripe_session_by_order_for_update(
        self, order_id: int
    ) -> StripeCheckoutSession | None:
        stmt = (
            select(StripeCheckoutSession)
            .where(StripeCheckoutSession.order_id == order_id)
            .order_by(StripeCheckoutSession.id.desc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def is_stripe_event_processed(self, stripe_event_id: str) -> bool:
        stmt = select(
            select(StripeProcessedEvent)
            .where(StripeProcessedEvent.stripe_event_id == stripe_event_id)
            .exists()
        )
        return bool(self._session.scalar(stmt))

    def record_stripe_event(
        self, event_record: StripeProcessedEvent
    ) -> StripeProcessedEvent:
        self._session.add(event_record)
        return event_record

    def record_outbox_event_idempotent(
        self,
        *,
        event_id: str,
        payment_attempt_id: int | None,
        order_id: int,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: datetime.datetime,
    ) -> int | None:
        """Записва outbox събитие за опита или поръчковото закритие идемпотентно."""
        stmt = pg_insert(PaymentOutboxEvent).values(
            event_id=event_id,
            event_version=1,
            payment_attempt_id=payment_attempt_id,
            order_id=order_id,
            event_type=event_type,
            payload=payload,
            status="pending",
            occurred_at=occurred_at,
        )
        if payment_attempt_id is not None:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["payment_attempt_id", "event_type"]
            )
        else:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["order_id", "event_type"],
                index_where=text("payment_attempt_id IS NULL"),
            )
        return self._session.execute(
            stmt.returning(PaymentOutboxEvent.id)
        ).scalar_one_or_none()

    def list_pending_outbox_events(
        self, limit: int = 50
    ) -> Sequence[PaymentOutboxEvent]:
        stmt = (
            select(PaymentOutboxEvent)
            .where(PaymentOutboxEvent.status == "pending")
            .order_by(PaymentOutboxEvent.created_at.asc(), PaymentOutboxEvent.id.asc())
            .limit(limit)
        )
        return self._session.scalars(stmt).all()

    def get_outbox_event_by_id_for_update(
        self, outbox_id: int
    ) -> PaymentOutboxEvent | None:
        stmt = (
            select(PaymentOutboxEvent)
            .where(PaymentOutboxEvent.id == outbox_id)
            .with_for_update()
        )
        return self._session.scalars(stmt).first()

    def list_unresolved_attempt_ids(self) -> Sequence[int]:
        stmt = (
            select(PaymentAttempt.id)
            .where(
                PaymentAttempt.status.in_(
                    ["initiated", "open", "processing", "cancelling"]
                )
            )
            .order_by(PaymentAttempt.id.asc())
        )
        return self._session.scalars(stmt).all()

    def list_transient_order_closure_ids(self) -> Sequence[int]:
        """Връща всички поръчки с прекъснато временно затваряне (cancelling/expiring)."""
        stmt = (
            select(PaymentOrderClosure.order_id)
            .where(PaymentOrderClosure.status.in_(["cancelling", "expiring"]))
            .order_by(PaymentOrderClosure.order_id.asc())
        )
        return self._session.scalars(stmt).all()

    def get_order_closure_for_update(self, order_id: int) -> PaymentOrderClosure | None:
        stmt = (
            select(PaymentOrderClosure)
            .where(PaymentOrderClosure.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def record_order_closure_idempotent(
        self, *, order_id: int, status: str, reason: str
    ) -> None:
        stmt = (
            pg_insert(PaymentOrderClosure)
            .values(order_id=order_id, status=status, reason=reason)
            .on_conflict_do_nothing(index_elements=["order_id"])
        )
        self._session.execute(stmt)

    def upsert_transient_order_closure(
        self, *, order_id: int, status: str, reason: str
    ) -> None:
        """Записва или обновява временно затваряне (cancelling/expiring), без да презаписва финално."""
        now = datetime.datetime.now(datetime.UTC)
        stmt = (
            pg_insert(PaymentOrderClosure)
            .values(order_id=order_id, status=status, reason=reason, closed_at=now)
            .on_conflict_do_update(
                index_elements=["order_id"],
                set_={"status": status, "reason": reason, "closed_at": now},
                where=PaymentOrderClosure.status.in_(["cancelling", "expiring"]),
            )
        )
        self._session.execute(stmt)

    def finalize_order_closure(
        self, *, order_id: int, status: str, reason: str
    ) -> None:
        """Повишава временното затваряне (cancelling/expiring) до окончателно (cancelled/expired)."""
        now = datetime.datetime.now(datetime.UTC)
        stmt = (
            pg_insert(PaymentOrderClosure)
            .values(order_id=order_id, status=status, reason=reason, closed_at=now)
            .on_conflict_do_update(
                index_elements=["order_id"],
                set_={"status": status, "reason": reason, "closed_at": now},
                where=PaymentOrderClosure.status.in_(["cancelling", "expiring"]),
            )
        )
        self._session.execute(stmt)

    def clear_transient_order_closure(self, order_id: int) -> None:
        """Премахва само временното затваряне (cancelling/expiring) при неуспешна отмяна или победа на плащането."""
        stmt = delete(PaymentOrderClosure).where(
            PaymentOrderClosure.order_id == order_id,
            PaymentOrderClosure.status.in_(["cancelling", "expiring"]),
        )
        self._session.execute(stmt)

    def has_discrepancy(
        self,
        *,
        payment_attempt_id: int,
        reason: str,
        source: str | None = None,
    ) -> bool:
        """Проверява дали вече съществува диагностично разхождение за този опит и причина."""
        conditions = [
            PaymentDiscrepancy.payment_attempt_id == payment_attempt_id,
            PaymentDiscrepancy.reason == reason,
        ]
        if source is not None:
            conditions.append(PaymentDiscrepancy.source == source)
        stmt = select(exists().where(*conditions))
        return bool(self._session.scalar(stmt))

    def record_discrepancy(self, discrepancy: PaymentDiscrepancy) -> PaymentDiscrepancy:
        self._session.add(discrepancy)
        return discrepancy

    def sync_sequences(self) -> None:
        """Синхронизира PostgreSQL sequence броячите след пренос на исторически данни."""
        for table in (
            "payment_attempts",
            "stripe_checkout_sessions",
            "stripe_processed_events",
            "payment_outbox_events",
            "payment_discrepancies",
        ):
            self._session.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"coalesce(max(id), 1), max(id) IS NOT NULL) FROM {table};"
                )
            )

    def count_records(self) -> dict[str, int]:
        return {
            table_name: int(
                self._session.scalar(select(func.count()).select_from(model)) or 0
            )
            for table_name, model in (
                ("payment_attempts", PaymentAttempt),
                ("stripe_checkout_sessions", StripeCheckoutSession),
                ("stripe_processed_events", StripeProcessedEvent),
                ("payment_outbox_events", PaymentOutboxEvent),
                ("payment_discrepancies", PaymentDiscrepancy),
            )
        }
