"""Независим worker за доставка по канали Email (SMTP) и SMS (Twilio адаптер)."""

import datetime
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.email_sender import EmailSender, EmailSenderProtocol
from app.repository import ClaimedDelivery, NotificationRepository
from app.sms_sender import (
    SmsSenderProtocol,
    TwilioSmsProvider,
    format_order_paid_sms,
    format_shipment_shipped_sms,
)

logger = logging.getLogger(__name__)

_EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PHONE_PATTERN = re.compile(r"\+?\d[\d\s\-()]{5,}\d")


def _sanitize_error_message(exc: Exception, payload: dict[str, Any]) -> str:
    """Санитизира текста на грешката, за да не съдържа имейли или телефонни номера."""
    message = f"{type(exc).__name__}: {exc}"
    for key in ("customer_email", "customer_phone"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            message = message.replace(val.strip(), "[REDACTED]")
    message = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", message)
    message = _PHONE_PATTERN.sub("[REDACTED_PHONE]", message)
    return message[:500]


class DeliveryWorker:
    """Обработва задачите за Email и SMS независимо с последователен захват преди изпращане.

    Всяко следващо задание се заключва непосредствено преди изпращането му (limit=1),
    така че времето за бавни предходни доставки не изяжда срока на захвата (lease) на
    следващите задачи в партидата. Ако SMTP приеме писмото миг преди падане на процеса
    преди commit на статуса 'sent', остава валидно документираното at-least-once повторение.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        email_sender: EmailSenderProtocol | None = None,
        sms_sender: SmsSenderProtocol | None = None,
        email_retry_delay_seconds: int | None = None,
        sms_retry_delay_seconds: int | None = None,
        delivery_lease_seconds: int | None = None,
        provider_timeout_seconds: float | None = None,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        if provider_timeout_seconds is not None and provider_timeout_seconds <= 0:
            raise ValueError("provider_timeout_seconds must be positive")

        self._session_factory = session_factory
        self._email_sender = email_sender or EmailSender(
            timeout=provider_timeout_seconds
        )
        self._sms_sender = sms_sender or TwilioSmsProvider(
            timeout=provider_timeout_seconds
        )
        self._email_retry_delay_seconds = (
            email_retry_delay_seconds
            if email_retry_delay_seconds is not None
            else settings.email_retry_delay_seconds
        )
        self._sms_retry_delay_seconds = (
            sms_retry_delay_seconds
            if sms_retry_delay_seconds is not None
            else settings.sms_retry_delay_seconds
        )
        self._delivery_lease_seconds = (
            delivery_lease_seconds
            if delivery_lease_seconds is not None
            else settings.delivery_lease_seconds
        )
        self._clock = clock or (lambda: datetime.datetime.now(datetime.UTC))

        self._validate_provider_timeouts_against_lease(provider_timeout_seconds)

    def _validate_provider_timeouts_against_lease(
        self, explicit_timeout: float | None
    ) -> None:
        timeouts: list[float] = []
        if explicit_timeout is not None:
            timeouts.append(float(explicit_timeout))
        for sender in (self._email_sender, self._sms_sender):
            raw_timeout = getattr(sender, "timeout", None)
            if isinstance(raw_timeout, (int, float)) and not isinstance(
                raw_timeout, bool
            ):
                if raw_timeout <= 0:
                    raise ValueError("Provider timeout must be positive")
                timeouts.append(float(raw_timeout))

        if timeouts:
            max_timeout = max(timeouts)
            if self._delivery_lease_seconds < max_timeout * 3:
                raise ValueError(
                    "delivery_lease_seconds must be at least 3x provider timeout "
                    f"({self._delivery_lease_seconds}s < {max_timeout * 3:.1f}s)"
                )

    def _deliver_email(self, claimed: ClaimedDelivery) -> str | None:
        to_email = str(claimed.payload.get("customer_email") or "")
        if not to_email:
            raise ValueError("missing_customer_email")

        if claimed.event_type == "order.paid":
            self._email_sender.send_order_paid_email(
                to_email=to_email,
                order_id=int(claimed.payload["order_id"]),
                total_minor=int(claimed.payload["total_minor"]),
                currency=str(claimed.payload["currency"]),
            )
            return "smtp_accepted"

        if claimed.event_type == "shipment.shipped":
            self._email_sender.send_shipment_shipped_email(
                to_email=to_email,
                order_id=int(claimed.payload["order_id"]),
                shipment_id=int(claimed.payload["shipment_id"]),
                items=list(claimed.payload.get("items") or []),
            )
            return "smtp_accepted"

        raise ValueError(f"unsupported_event_type:{claimed.event_type}")

    def _deliver_sms(self, claimed: ClaimedDelivery) -> str | None:
        to_phone = str(claimed.payload.get("customer_phone") or "").strip()
        if not to_phone:
            raise ValueError("missing_customer_phone")

        if claimed.event_type == "order.paid":
            sms_body = format_order_paid_sms(
                order_id=int(claimed.payload["order_id"]),
                total_minor=int(claimed.payload["total_minor"]),
                currency=str(claimed.payload["currency"]),
            )
        elif claimed.event_type == "shipment.shipped":
            items = list(claimed.payload.get("items") or [])
            sms_body = format_shipment_shipped_sms(
                order_id=int(claimed.payload["order_id"]),
                shipment_id=int(claimed.payload["shipment_id"]),
                items_count=len(items),
            )
        else:
            raise ValueError(f"unsupported_event_type:{claimed.event_type}")

        result = self._sms_sender.send_sms(
            to_phone=to_phone,
            message=sms_body,
            idempotency_key=f"{claimed.event_id}:sms",
        )
        return result.provider_message_id

    def process_once(
        self,
        *,
        limit: int = 20,
        now: datetime.datetime | None = None,
    ) -> dict[str, int]:
        """Последователно заключва по 1 задача непосредствено преди изпращане и я финализира."""
        stats = {
            "claimed": 0,
            "sent": 0,
            "retried": 0,
            "failed": 0,
            "stale": 0,
        }
        attempted_ids: set[int] = set()

        while len(attempted_ids) < limit:
            claim_time = now if now is not None else self._clock()
            with self._session_factory() as session:
                repo = NotificationRepository(session)
                claimed_batch = repo.claim_due_deliveries(
                    limit=1,
                    lease_seconds=self._delivery_lease_seconds,
                    now=claim_time,
                    exclude_delivery_ids=attempted_ids,
                )
                session.commit()

            if not claimed_batch:
                break

            task = claimed_batch[0]
            attempted_ids.add(task.delivery_id)
            stats["claimed"] += 1

            try:
                if task.channel == "email":
                    provider_id = self._deliver_email(task)
                elif task.channel == "sms":
                    provider_id = self._deliver_sms(task)
                else:
                    raise ValueError(f"unsupported_channel:{task.channel}")
            except Exception as exc:  # noqa: BLE001
                safe_error = _sanitize_error_message(exc, task.payload)
                retry_delay = (
                    self._email_retry_delay_seconds
                    if task.channel == "email"
                    else self._sms_retry_delay_seconds
                )
                finalize_time = now if now is not None else self._clock()
                with self._session_factory() as session:
                    repo = NotificationRepository(session)
                    outcome = repo.finalize_delivery_failure(
                        task,
                        error_message=safe_error,
                        retry_delay_seconds=retry_delay,
                        now=finalize_time,
                    )
                    session.commit()

                logger.warning(
                    "Delivery failed delivery_id=%s event_id=%s order_id=%s shipment_id=%s channel=%s attempt=%s/%s outcome=%s error=%s",
                    task.delivery_id,
                    task.event_id,
                    task.order_id,
                    task.shipment_id,
                    task.channel,
                    task.attempt_count,
                    task.max_attempts,
                    outcome,
                    safe_error,
                )
                if outcome == "retry_scheduled":
                    stats["retried"] += 1
                elif outcome == "failed":
                    stats["failed"] += 1
                else:
                    stats["stale"] += 1
                continue

            finalize_time = now if now is not None else self._clock()
            with self._session_factory() as session:
                repo = NotificationRepository(session)
                finalized = repo.finalize_delivery_success(
                    task,
                    provider_message_id=provider_id,
                    now=finalize_time,
                )
                session.commit()

            if finalized:
                logger.info(
                    "Delivered notification delivery_id=%s event_id=%s order_id=%s shipment_id=%s channel=%s attempt=%s reclaimed=%s provider_ref=%s",
                    task.delivery_id,
                    task.event_id,
                    task.order_id,
                    task.shipment_id,
                    task.channel,
                    task.attempt_count,
                    task.reclaimed,
                    provider_id,
                )
                stats["sent"] += 1
            else:
                stats["stale"] += 1

        return stats

    def run_loop(
        self,
        stop_event: threading.Event,
        *,
        poll_interval_seconds: float | None = None,
        batch_size: int = 20,
    ) -> None:
        """Непрекъснат цикъл на worker-а за Email и SMS доставки."""
        interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else settings.worker_poll_interval_seconds
        )
        while not stop_event.is_set():
            try:
                stats = self.process_once(limit=batch_size)
                if stats["claimed"] > 0:
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "DeliveryWorker loop error=%s",
                    _sanitize_error_message(exc, {}),
                )
            stop_event.wait(interval)
