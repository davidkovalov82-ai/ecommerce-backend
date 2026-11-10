"""Идемпотентен скрипт за пренос на платёжно състояние от Commerce БД към Payments БД при преминаване към К5."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.models import (
    PaymentAttempt,
    StripeCheckoutSession,
    StripeProcessedEvent,
)
from app.repository import PaymentRepository

_SESSION_TO_ATTEMPT_STATUS = {
    "completed": "succeeded",
    "initiated": "initiated",
    "open": "open",
    "processing": "processing",
    "cancelling": "cancelling",
    "expired": "expired",
    "failed": "failed",
}


def migrate_commerce_payment_state(
    commerce_engine: Engine,
    payments_engine: Engine,
) -> dict[str, int]:
    """Пренася отворени/исторически Stripe сесии, опити и обработени event ID без триене на старите таблици."""
    stats = {
        "sessions_inserted": 0,
        "legacy_attempts_inserted": 0,
        "processed_events_inserted": 0,
    }

    with commerce_engine.connect() as c_conn:
        sessions_rows: list[Any] = (
            c_conn.execute(
                text(
                    """
                    SELECT s.id, s.order_id, s.status, s.amount_minor, s.currency,
                           s.stripe_session_id, s.checkout_url, s.idempotency_key,
                           s.mode, s.created_at, s.updated_at, o.expires_at AS order_expires_at
                    FROM stripe_checkout_sessions s
                    JOIN orders o ON o.id = s.order_id
                    ORDER BY s.id ASC
                    """
                )
            )
            .mappings()
            .all()
        )

        legacy_attempt_rows: list[Any] = (
            c_conn.execute(
                text(
                    """
                    SELECT a.id, a.order_id, a.payment_method, a.status,
                           a.amount_minor, a.currency, a.idempotency_key, a.created_at,
                           o.expires_at AS order_expires_at
                    FROM payment_attempts a
                    JOIN orders o ON o.id = a.order_id
                    ORDER BY a.id ASC
                    """
                )
            )
            .mappings()
            .all()
        )

        processed_event_rows: list[Any] = (
            c_conn.execute(
                text(
                    """
                    SELECT id, stripe_event_id, event_type, order_id, created_at
                    FROM stripe_processed_events
                    ORDER BY id ASC
                    """
                )
            )
            .mappings()
            .all()
        )

    with Session(payments_engine) as p_session:
        # 1. Пренос на stripe_checkout_sessions + съответстващи payment_attempts
        for row in sessions_rows:
            existing_session = p_session.scalar(
                select(StripeCheckoutSession).where(
                    StripeCheckoutSession.idempotency_key == row["idempotency_key"]
                )
            )
            if existing_session is None and row["stripe_session_id"] is not None:
                existing_session = p_session.scalar(
                    select(StripeCheckoutSession).where(
                        StripeCheckoutSession.stripe_session_id
                        == row["stripe_session_id"]
                    )
                )
            if existing_session is not None:
                continue

            attempt_status = _SESSION_TO_ATTEMPT_STATUS.get(row["status"], "expired")
            existing_attempt = p_session.scalar(
                select(PaymentAttempt).where(
                    PaymentAttempt.order_id == row["order_id"],
                    PaymentAttempt.attempt_key == row["idempotency_key"],
                )
            )
            if existing_attempt is None:
                attempt_stmt = (
                    pg_insert(PaymentAttempt)
                    .values(
                        order_id=row["order_id"],
                        attempt_key=row["idempotency_key"],
                        payment_method="stripe_checkout",
                        status=attempt_status,
                        amount_minor=row["amount_minor"],
                        currency=row["currency"],
                        mode=row["mode"],
                        line_items=[],
                        order_expires_at=row["order_expires_at"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                    .on_conflict_do_nothing(index_elements=["order_id", "attempt_key"])
                    .returning(PaymentAttempt.id)
                )
                payment_attempt_id = p_session.execute(
                    attempt_stmt
                ).scalar_one_or_none()
                if payment_attempt_id is None:
                    payment_attempt_id = p_session.scalar(
                        select(PaymentAttempt.id).where(
                            PaymentAttempt.order_id == row["order_id"],
                            PaymentAttempt.attempt_key == row["idempotency_key"],
                        )
                    )
            else:
                payment_attempt_id = existing_attempt.id

            if payment_attempt_id is None:
                continue

            session_values: dict[str, Any] = {
                "payment_attempt_id": payment_attempt_id,
                "order_id": row["order_id"],
                "status": row["status"],
                "amount_minor": row["amount_minor"],
                "currency": row["currency"],
                "stripe_session_id": row["stripe_session_id"],
                "checkout_url": row["checkout_url"],
                "idempotency_key": row["idempotency_key"],
                "mode": row["mode"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            if p_session.get(StripeCheckoutSession, row["id"]) is None:
                session_values["id"] = row["id"]
                conflict_elements = ["id"]
            else:
                conflict_elements = ["idempotency_key"]

            session_stmt = (
                pg_insert(StripeCheckoutSession)
                .values(**session_values)
                .on_conflict_do_nothing(index_elements=conflict_elements)
                .returning(StripeCheckoutSession.id)
            )
            inserted_sid = p_session.execute(session_stmt).scalar_one_or_none()
            if inserted_sid is not None:
                stats["sessions_inserted"] += 1

        # 2. Пренос на старите payment_attempts (К2 опити) без конфликт със вече пренесените успешни
        for row in legacy_attempt_rows:
            if row["status"] == "succeeded":
                already_succeeded = p_session.scalar(
                    select(PaymentAttempt.id).where(
                        PaymentAttempt.order_id == row["order_id"],
                        PaymentAttempt.status == "succeeded",
                    )
                )
                if already_succeeded is not None:
                    continue

            attempt_stmt = (
                pg_insert(PaymentAttempt)
                .values(
                    order_id=row["order_id"],
                    attempt_key=row["idempotency_key"],
                    payment_method=row["payment_method"],
                    status=row["status"],
                    amount_minor=row["amount_minor"],
                    currency=row["currency"],
                    mode="test",
                    line_items=[],
                    order_expires_at=row["order_expires_at"],
                    created_at=row["created_at"],
                    updated_at=row["created_at"],
                )
                .on_conflict_do_nothing(index_elements=["order_id", "attempt_key"])
                .returning(PaymentAttempt.id)
            )
            if p_session.execute(attempt_stmt).scalar_one_or_none() is not None:
                stats["legacy_attempts_inserted"] += 1

        # 3. Пренос на обработените Stripe event ID
        for row in processed_event_rows:
            ev_stmt = (
                pg_insert(StripeProcessedEvent)
                .values(
                    stripe_event_id=row["stripe_event_id"],
                    event_type=row["event_type"],
                    order_id=row["order_id"],
                    payment_attempt_id=None,
                    created_at=row["created_at"],
                )
                .on_conflict_do_nothing(index_elements=["stripe_event_id"])
                .returning(StripeProcessedEvent.id)
            )
            if p_session.execute(ev_stmt).scalar_one_or_none() is not None:
                stats["processed_events_inserted"] += 1

        PaymentRepository(p_session).sync_sequences()
        p_session.commit()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Пренос на отворени Stripe сесии, опити и обработени event ID от Commerce към Payments."
    )
    parser.add_argument(
        "--commerce-url",
        required=True,
        help="PostgreSQL URL на изходната Commerce БД (използва се само при миграцията).",
    )
    parser.add_argument(
        "--payments-url",
        default=settings.database_url,
        help="PostgreSQL URL на целевата Payments БД (по подразбиране от settings).",
    )
    args = parser.parse_args()

    c_engine = create_engine(args.commerce_url, pool_pre_ping=True)
    p_engine = create_engine(args.payments_url, pool_pre_ping=True)
    try:
        stats = migrate_commerce_payment_state(c_engine, p_engine)
        print(
            "Payment state migration completed: "
            f"sessions_inserted={stats['sessions_inserted']}, "
            f"legacy_attempts_inserted={stats['legacy_attempts_inserted']}, "
            f"processed_events_inserted={stats['processed_events_inserted']}"
        )
    finally:
        c_engine.dispose()
        p_engine.dispose()


if __name__ == "__main__":
    main()
