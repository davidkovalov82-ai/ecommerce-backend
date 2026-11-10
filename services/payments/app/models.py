"""SQLAlchemy ORM модели, притежавани единствено от Payments сервиса."""

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PaymentAttempt(Base):
    """Платёжна попытка за поръчка (order_id е контрактна препратка без FK)."""

    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "attempt_key",
            name="uq_payment_attempts_order_id_attempt_key",
        ),
        Index(
            "uq_payment_attempts_one_succeeded_per_order",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
        ),
        Index(
            "uq_payment_attempts_one_active_per_order",
            "order_id",
            unique=True,
            postgresql_where=text(
                "status IN ('initiated', 'open', 'processing', 'cancelling')"
            ),
        ),
        CheckConstraint(
            "status IN ('initiated', 'open', 'processing', 'succeeded', 'cancelling', 'failed', 'expired', 'declined')",
            name="ck_payment_attempts_status_valid",
        ),
        CheckConstraint(
            "amount_minor >= 0",
            name="ck_payment_attempts_amount_minor_non_negative",
        ),
        CheckConstraint("currency = 'EUR'", name="ck_payment_attempts_currency_eur"),
        CheckConstraint(
            "mode IN ('test', 'live')", name="ck_payment_attempts_mode_valid"
        ),
        Index("ix_payment_attempts_order_id", "order_id"),
        Index("ix_payment_attempts_status_expires", "status", "order_expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_key: Mapped[str] = mapped_column(String(100), nullable=False)
    payment_method: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="stripe_checkout",
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="initiated")
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    mode: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="test",
        server_default="test",
    )
    line_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    order_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.datetime.now(datetime.UTC),
    )

    stripe_session: Mapped["StripeCheckoutSession | None"] = relationship(
        "StripeCheckoutSession",
        back_populates="payment_attempt",
        uselist=False,
        cascade="all, delete-orphan",
    )


class StripeCheckoutSession(Base):
    """Stripe Checkout Session, обвързана с конкретен payment_attempt_id."""

    __tablename__ = "stripe_checkout_sessions"
    __table_args__ = (
        UniqueConstraint(
            "payment_attempt_id",
            name="uq_stripe_checkout_sessions_attempt_id",
        ),
        UniqueConstraint(
            "stripe_session_id",
            name="uq_stripe_checkout_sessions_session_id",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_stripe_checkout_sessions_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('initiated', 'open', 'processing', 'completed', 'cancelling', 'expired', 'failed')",
            name="ck_stripe_checkout_sessions_status_valid",
        ),
        CheckConstraint(
            "mode IN ('test', 'live')",
            name="ck_stripe_checkout_sessions_mode_valid",
        ),
        CheckConstraint(
            "amount_minor >= 0",
            name="ck_stripe_checkout_sessions_amount_minor_non_negative",
        ),
        CheckConstraint(
            "currency = 'EUR'",
            name="ck_stripe_checkout_sessions_currency_eur",
        ),
        Index("ix_stripe_checkout_sessions_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    payment_attempt_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("payment_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="initiated")
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    stripe_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checkout_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="test",
        server_default="test",
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.datetime.now(datetime.UTC),
    )

    payment_attempt: Mapped["PaymentAttempt"] = relationship(
        "PaymentAttempt",
        back_populates="stripe_session",
    )


class StripeProcessedEvent(Base):
    """Идемпотентен дневник на обработените Stripe webhook event ID."""

    __tablename__ = "stripe_processed_events"
    __table_args__ = (
        UniqueConstraint(
            "stripe_event_id",
            name="uq_stripe_processed_events_event_id",
        ),
        Index("ix_stripe_processed_events_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stripe_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payment_attempt_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )


class PaymentOutboxEvent(Base):
    """Transactional Outbox на Payments за изходящи събития към Commerce през RabbitMQ."""

    __tablename__ = "payment_outbox_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_payment_outbox_event_id"),
        UniqueConstraint(
            "payment_attempt_id",
            "event_type",
            name="uq_payment_outbox_attempt_event",
        ),
        CheckConstraint(
            "event_type IN ('payment.succeeded', 'payment.processing', 'payment.failed', 'payment.expired')",
            name="ck_payment_outbox_event_type_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'published', 'failed')",
            name="ck_payment_outbox_status_valid",
        ),
        Index("ix_payment_outbox_status_created", "status", "created_at"),
        Index("ix_payment_outbox_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        default=lambda: str(uuid.uuid4()),
        server_default=text("gen_random_uuid()::text"),
    )
    event_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    payment_attempt_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    processed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )


class PaymentOrderClosure(Base):
    """Окончателно затваряне на плащанията за отказана или просрочена поръчка."""

    __tablename__ = "payment_order_closures"
    __table_args__ = (
        CheckConstraint(
            "status IN ('cancelling', 'expiring', 'cancelled', 'expired')",
            name="ck_payment_order_closures_status_valid",
        ),
    )

    order_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    closed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )


class PaymentDiscrepancy(Base):
    """Диагностичен дневник за несъответствия при проверка на потвърдено Stripe плащане."""

    __tablename__ = "payment_discrepancies"
    __table_args__ = (
        CheckConstraint(
            "source IN ('webhook', 'reconcile', 'cancel_conflict', 'checkout')",
            name="ck_payment_discrepancies_source_valid",
        ),
        Index("ix_payment_discrepancies_order_id", "order_id"),
        Index("ix_payment_discrepancies_attempt_id", "payment_attempt_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payment_attempt_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stripe_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    expected_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actual_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    actual_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )
