"""SQLAlchemy ORM модели на входящите събития и каналните задания за доставка."""

import datetime
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


class InboxEvent(Base):
    """Идемпотентен журнал на приетите събития от RabbitMQ (Inbox Pattern)."""

    __tablename__ = "inbox_events"
    __table_args__ = (
        Index("ix_inbox_events_order_id", "order_id"),
        Index("ix_inbox_events_shipment_id", "shipment_id"),
        Index("ix_inbox_events_received_at", "received_at"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shipment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )

    deliveries: Mapped[list["NotificationDelivery"]] = relationship(
        "NotificationDelivery",
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="NotificationDelivery.id.asc()",
    )


class NotificationDelivery(Base):
    """Независимо задание за доставка на известие по конкретен канал (email или sms)."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "channel",
            name="uq_notification_deliveries_event_channel",
        ),
        CheckConstraint(
            "channel IN ('email', 'sms')",
            name="ck_notification_deliveries_channel_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed', 'skipped')",
            name="ck_notification_deliveries_status_valid",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_notification_deliveries_attempt_non_negative",
        ),
        CheckConstraint(
            "max_attempts >= 1",
            name="ck_notification_deliveries_max_attempts_positive",
        ),
        Index("ix_notification_deliveries_due", "status", "next_retry_at"),
        Index("ix_notification_deliveries_locked_until", "locked_until"),
        Index("ix_notification_deliveries_event_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("inbox_events.event_id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        server_default=text("5"),
    )
    next_retry_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
        server_default=func.now(),
    )
    locked_until: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    processing_token: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
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
    )

    event: Mapped["InboxEvent"] = relationship(
        "InboxEvent",
        back_populates="deliveries",
    )
