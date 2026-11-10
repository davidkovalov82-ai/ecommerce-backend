"""Бизнес логика на Payments сервиса: Checkout сесии, Stripe webhooks и сверка."""

import datetime
import logging
import uuid
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    PaymentAttempt,
    PaymentDiscrepancy,
    StripeCheckoutSession,
    StripeProcessedEvent,
)

logger = logging.getLogger(__name__)
from app.repository import PaymentRepository
from app.schemas import (
    InternalCancelPaymentRequest,
    InternalCancelPaymentResponse,
    InternalCheckoutSessionRequest,
    InternalCheckoutSessionResponse,
)
from app.stripe_client import StripeClient, StripeNotConfigured


class InvalidPaymentAmount(Exception):
    """Невалидна сума спрямо ограниченията на Stripe."""


class OrderAlreadyPaid(Exception):
    """Поръчката вече има потвърдено успешно плащане."""


class AttemptTerminalError(Exception):
    """Опитът с този attempt_key вече е завършил неуспешно или е изтекъл."""


class PaymentStateConflict(Exception):
    """Конфликт със текущото състояние на платежната сесия/опит."""

    def __init__(self, current_status: str, message: str) -> None:
        self.current_status = current_status
        super().__init__(message)


class StripeServiceError(Exception):
    """Грешка или таймаут при комуникация със Stripe API."""


class PaymentService:
    """Управлява платежните опити, Stripe Checkout сесиите и Payment Outbox събитията."""

    def __init__(
        self,
        *,
        repository: PaymentRepository,
        session: Session,
        stripe_client: StripeClient | None = None,
    ) -> None:
        self._repo = repository
        self._session = session
        self._stripe_client = stripe_client or StripeClient()

    def _enqueue_outbox_event(
        self,
        *,
        attempt: PaymentAttempt,
        session_record: StripeCheckoutSession | None,
        event_type: str,
        stripe_event_id: str | None = None,
        occurred_at: datetime.datetime | None = None,
    ) -> None:
        now = occurred_at or datetime.datetime.now(datetime.UTC)
        payload = {
            "order_id": attempt.order_id,
            "payment_attempt_id": attempt.id,
            "attempt_key": attempt.attempt_key,
            "stripe_session_id": (
                session_record.stripe_session_id if session_record else None
            ),
            "stripe_event_id": stripe_event_id,
            "amount_minor": attempt.amount_minor,
            "currency": attempt.currency.upper(),
            "mode": attempt.mode,
        }
        self._repo.record_outbox_event_idempotent(
            event_id=str(uuid.uuid4()),
            payment_attempt_id=attempt.id,
            order_id=attempt.order_id,
            event_type=event_type,
            payload=payload,
            occurred_at=now,
        )

    def _record_discrepancy(
        self,
        *,
        source: Literal["webhook", "reconcile", "cancel_conflict"],
        reason: str,
        order_id: int | None = None,
        payment_attempt_id: int | None = None,
        stripe_session_id: str | None = None,
        stripe_event_id: str | None = None,
        expected_amount_minor: int | None = None,
        actual_amount_minor: int | None = None,
        expected_currency: str | None = None,
        actual_currency: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        logger.warning(
            "Payment validation discrepancy source=%s reason=%s order_id=%s payment_attempt_id=%s",
            source,
            reason,
            order_id,
            payment_attempt_id,
        )
        self._repo.record_discrepancy(
            PaymentDiscrepancy(
                order_id=order_id,
                payment_attempt_id=payment_attempt_id,
                stripe_session_id=stripe_session_id,
                stripe_event_id=stripe_event_id,
                source=source,
                reason=reason,
                expected_amount_minor=expected_amount_minor,
                actual_amount_minor=actual_amount_minor,
                expected_currency=expected_currency,
                actual_currency=actual_currency,
                details=details or {},
            )
        )

    def _validate_and_record_paid_discrepancy(
        self,
        *,
        source: Literal["webhook", "reconcile", "cancel_conflict"],
        attempt: PaymentAttempt,
        session_rec: StripeCheckoutSession,
        session_obj: Any,
        stripe_event_id: str | None = None,
        event_livemode: bool | None = None,
    ) -> str | None:
        """Единна проверка на потвърдено Stripe плащане (режим, чужда сесия, сума, валута)."""
        expected_livemode = settings.stripe_mode == "live"
        retrieved_sid = _read_session_field(session_obj, "id", None)
        session_livemode = _read_session_field(session_obj, "livemode", None)
        raw_amount = _read_session_field(session_obj, "amount_total", None)
        actual_amount = (
            raw_amount
            if isinstance(raw_amount, int) and not isinstance(raw_amount, bool)
            else None
        )
        raw_currency = _read_session_field(session_obj, "currency", None)
        actual_currency = (
            str(raw_currency).upper() if raw_currency is not None else None
        )

        mismatch_reason: str | None = None
        if (
            event_livemode is not None and bool(event_livemode) != expected_livemode
        ) or (
            session_livemode is not None and bool(session_livemode) != expected_livemode
        ):
            mismatch_reason = "mode_mismatch"
        elif (
            attempt.mode != settings.stripe_mode
            or session_rec.mode != settings.stripe_mode
        ):
            mismatch_reason = "attempt_mode_mismatch"
        elif (
            retrieved_sid is not None
            and session_rec.stripe_session_id is not None
            and str(retrieved_sid) != session_rec.stripe_session_id
        ):
            mismatch_reason = "session_id_mismatch"
        else:
            client_ref = _read_session_field(session_obj, "client_reference_id", None)
            if (
                client_ref is not None
                and _parse_optional_int(client_ref) != attempt.order_id
            ):
                mismatch_reason = "session_order_mismatch"
            else:
                metadata = _read_session_field(session_obj, "metadata", None)
                meta_oid = (
                    _read_session_field(metadata, "order_id", None)
                    if metadata is not None
                    else None
                )
                meta_ak = (
                    _read_session_field(metadata, "attempt_key", None)
                    if metadata is not None
                    else None
                )
                meta_paid = (
                    _read_session_field(metadata, "payment_attempt_id", None)
                    if metadata is not None
                    else None
                )
                if (
                    meta_oid is not None
                    and _parse_optional_int(meta_oid) != attempt.order_id
                ):
                    mismatch_reason = "session_order_mismatch"
                elif meta_ak is not None and str(meta_ak) != attempt.attempt_key:
                    mismatch_reason = "session_attempt_key_mismatch"
                elif (
                    meta_paid is not None
                    and _parse_optional_int(meta_paid) != attempt.id
                ):
                    mismatch_reason = "session_attempt_id_mismatch"

        if mismatch_reason is None and (
            actual_amount is None
            or actual_amount != attempt.amount_minor
            or actual_amount != session_rec.amount_minor
        ):
            mismatch_reason = "amount_mismatch"

        if mismatch_reason is None and (
            not actual_currency
            or actual_currency != attempt.currency.upper()
            or actual_currency != session_rec.currency.upper()
        ):
            mismatch_reason = "currency_mismatch"

        if mismatch_reason is not None:
            self._record_discrepancy(
                source=source,
                reason=mismatch_reason,
                order_id=attempt.order_id,
                payment_attempt_id=attempt.id,
                stripe_session_id=str(retrieved_sid)
                if retrieved_sid is not None
                else session_rec.stripe_session_id,
                stripe_event_id=stripe_event_id,
                expected_amount_minor=attempt.amount_minor,
                actual_amount_minor=actual_amount,
                expected_currency=attempt.currency.upper(),
                actual_currency=actual_currency,
                details={
                    "expected_mode": settings.stripe_mode,
                    "session_livemode": session_livemode,
                    "expected_stripe_session_id": session_rec.stripe_session_id,
                },
            )
            return mismatch_reason
        return None

    def _complete_verified_payment(
        self,
        *,
        attempt: PaymentAttempt,
        session_rec: StripeCheckoutSession,
        retrieved_sid: str | None = None,
        stripe_event_id: str | None = None,
    ) -> None:
        if session_rec.stripe_session_id is None and retrieved_sid:
            session_rec.stripe_session_id = retrieved_sid
        attempt.status = "succeeded"
        session_rec.status = "completed"
        self._enqueue_outbox_event(
            attempt=attempt,
            session_record=session_rec,
            event_type="payment.succeeded",
            stripe_event_id=stripe_event_id,
        )

    def _record_ignored_stripe_event(
        self,
        *,
        event_id: str,
        event_type: str,
        order_id: int | None = None,
        payment_attempt_id: int | None = None,
        response_status: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        try:
            self._repo.record_stripe_event(
                StripeProcessedEvent(
                    stripe_event_id=event_id,
                    event_type=event_type,
                    order_id=order_id,
                    payment_attempt_id=payment_attempt_id,
                )
            )
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            if self._repo.is_stripe_event_processed(event_id):
                return {"status": "already_processed", "event_id": event_id}
            raise
        result: dict[str, Any] = {"status": response_status, "event_id": event_id}
        if reason is not None:
            result["reason"] = reason
        return result

    def create_checkout_session(
        self,
        req: InternalCheckoutSessionRequest,
    ) -> InternalCheckoutSessionResponse:
        """Създава или безопасно връща активна Stripe Checkout сесия без DB заключване при мрежовия I/O."""
        if req.amount_minor < 50:
            raise InvalidPaymentAmount(
                "Order total must be at least 50 cents (EUR 0.50) for Stripe Checkout"
            )
        if req.amount_minor > 99_999_999:
            raise InvalidPaymentAmount(
                "Order total exceeds Stripe's maximum allowed amount"
            )

        # Фаза 1: Синхронизация по order_id и запис в 'initiated' преди заявка към Stripe
        try:
            self._repo.acquire_order_lock(req.order_id)

            if (
                self._repo.get_succeeded_attempt_by_order_for_update(req.order_id)
                is not None
            ):
                raise OrderAlreadyPaid("Order is already paid")

            closure = self._repo.get_order_closure_for_update(req.order_id)
            if closure is not None:
                self._session.commit()
                raise PaymentStateConflict(
                    current_status=closure.status,
                    message=f"Cannot create checkout session for order in status '{closure.status}'",
                )

            same_key_attempt = self._repo.get_attempt_by_order_and_key_for_update(
                req.order_id, req.attempt_key
            )
            if same_key_attempt is not None and same_key_attempt.status in (
                "failed",
                "expired",
                "declined",
            ):
                raise AttemptTerminalError(
                    f"Payment attempt key '{req.attempt_key}' already ended with status '{same_key_attempt.status}'"
                )

            active_attempt = self._repo.get_active_attempt_by_order_for_update(
                req.order_id
            )
            session_record: StripeCheckoutSession | None = None

            if active_attempt is not None:
                if req.order_expires_at is not None:
                    active_attempt.order_expires_at = req.order_expires_at
                session_record = self._repo.get_stripe_session_by_attempt_id_for_update(
                    active_attempt.id
                )

                if active_attempt.status in ("cancelling", "processing"):
                    self._session.commit()
                    raise PaymentStateConflict(
                        current_status=active_attempt.status,
                        message=f"Cannot create checkout session while payment is in status '{active_attempt.status}'",
                    )

                if (
                    active_attempt.status == "open"
                    and session_record is not None
                    and session_record.stripe_session_id
                    and session_record.checkout_url
                ):
                    result = InternalCheckoutSessionResponse(
                        payment_attempt_id=active_attempt.id,
                        session_id=session_record.stripe_session_id,
                        checkout_url=session_record.checkout_url,
                        status=session_record.status,
                    )
                    self._session.commit()
                    return result

                if active_attempt.status == "initiated":
                    if active_attempt.attempt_key != req.attempt_key:
                        self._session.commit()
                        raise PaymentStateConflict(
                            current_status="initiated",
                            message="Another checkout session initiation is already in progress",
                        )
                    attempt = active_attempt
                    if session_record is None:
                        session_record = StripeCheckoutSession(
                            payment_attempt_id=attempt.id,
                            order_id=req.order_id,
                            status="initiated",
                            amount_minor=attempt.amount_minor,
                            currency=attempt.currency,
                            mode=attempt.mode,
                            idempotency_key=attempt.attempt_key,
                        )
                        self._repo.add_stripe_session(session_record)
                        self._session.flush()
                else:
                    raise PaymentStateConflict(
                        current_status=active_attempt.status,
                        message=f"Unsupported active attempt status '{active_attempt.status}'",
                    )
            else:
                serialized_items = [
                    {
                        "price_data": {
                            "currency": item.currency.lower(),
                            "unit_amount": item.unit_amount,
                            "product_data": {"name": item.name},
                        },
                        "quantity": item.quantity,
                    }
                    for item in req.line_items
                ]
                attempt = PaymentAttempt(
                    order_id=req.order_id,
                    attempt_key=req.attempt_key,
                    payment_method="stripe_checkout",
                    status="initiated",
                    amount_minor=req.amount_minor,
                    currency=req.currency,
                    mode=settings.stripe_mode,
                    line_items=serialized_items,
                    order_expires_at=req.order_expires_at,
                )
                self._repo.add_attempt(attempt)
                self._session.flush()

                session_record = StripeCheckoutSession(
                    payment_attempt_id=attempt.id,
                    order_id=req.order_id,
                    status="initiated",
                    amount_minor=req.amount_minor,
                    currency=req.currency,
                    mode=settings.stripe_mode,
                    idempotency_key=req.attempt_key,
                )
                self._repo.add_stripe_session(session_record)
                self._session.flush()

            attempt_id = attempt.id
            session_db_id = session_record.id
            idempotency_key = session_record.idempotency_key
            stripe_line_items = list(attempt.line_items)
            attempt_key = attempt.attempt_key
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        # Фаза 2: Мрежова заявка към Stripe БЕЗ отворена транзакция и БЕЗ DB заключване
        success_url = settings.stripe_success_url.format(order_id=req.order_id)
        cancel_url = settings.stripe_cancel_url.format(order_id=req.order_id)
        try:
            stripe_session = self._stripe_client.create_checkout_session(
                line_items=stripe_line_items,
                success_url=success_url,
                cancel_url=cancel_url,
                metadata={
                    "order_id": str(req.order_id),
                    "attempt_id": str(session_db_id),
                    "payment_attempt_id": str(attempt_id),
                    "attempt_key": attempt_key,
                },
                client_reference_id=str(req.order_id),
                idempotency_key=idempotency_key,
            )
        except StripeNotConfigured:
            raise
        except Exception as exc:
            # Оставяме 'initiated' със стабилния idempotency_key -> повторен опит извлича същата сесия
            raise StripeServiceError(
                f"Stripe session creation failed: {type(exc).__name__}"
            ) from exc

        # Фаза 3: Запис на резултата от Stripe под кратко заключване
        try:
            self._repo.acquire_order_lock(req.order_id)
            attempt_rec = self._repo.get_attempt_by_id_for_update(attempt_id)
            session_rec = self._repo.get_stripe_session_by_id_for_update(session_db_id)

            if attempt_rec is not None and session_rec is not None:
                if session_rec.stripe_session_id is None:
                    session_rec.stripe_session_id = stripe_session.id
                if session_rec.checkout_url is None:
                    session_rec.checkout_url = stripe_session.url

                if (
                    attempt_rec.status == "succeeded"
                    or session_rec.status == "completed"
                ):
                    session_rec.status = "completed"
                    self._session.commit()
                    return InternalCheckoutSessionResponse(
                        payment_attempt_id=attempt_rec.id,
                        session_id=session_rec.stripe_session_id or stripe_session.id,
                        checkout_url=session_rec.checkout_url or stripe_session.url,
                        status="completed",
                    )

                closure_after = self._repo.get_order_closure_for_update(req.order_id)
                if (
                    closure_after is not None
                    or attempt_rec.status in ("cancelling", "expired")
                    or session_rec.status in ("cancelling", "expired")
                ):
                    attempt_rec.status = "cancelling"
                    session_rec.status = "cancelling"
                    self._session.commit()

                    try:
                        self._stripe_client.expire_checkout_session(stripe_session.id)
                    except Exception as exc:
                        if "already expired" not in str(exc).lower():
                            raise StripeServiceError(
                                f"Failed to expire late session in Stripe: {type(exc).__name__}"
                            ) from exc

                    self._repo.acquire_order_lock(req.order_id)
                    attempt_rec = self._repo.get_attempt_by_id_for_update(attempt_id)
                    session_rec = self._repo.get_stripe_session_by_id_for_update(
                        session_db_id
                    )
                    if attempt_rec is not None:
                        attempt_rec.status = "expired"
                    if session_rec is not None:
                        session_rec.status = "expired"
                    self._session.commit()
                    conflict_status = (
                        closure_after.status
                        if closure_after is not None
                        else "cancelled"
                    )
                    raise PaymentStateConflict(
                        current_status=conflict_status,
                        message="Order was cancelled while creating checkout session",
                    )

                if attempt_rec.status == "initiated":
                    attempt_rec.status = "open"
                    session_rec.status = "open"
                    self._session.commit()
                    return InternalCheckoutSessionResponse(
                        payment_attempt_id=attempt_rec.id,
                        session_id=stripe_session.id,
                        checkout_url=stripe_session.url,
                        status="open",
                    )

            self._session.commit()
            return InternalCheckoutSessionResponse(
                payment_attempt_id=attempt_id,
                session_id=stripe_session.id,
                checkout_url=stripe_session.url,
                status="open",
            )
        except Exception:
            self._session.rollback()
            raise

    def cancel_or_expire_order(
        self,
        order_id: int,
        req: InternalCancelPaymentRequest,
    ) -> InternalCancelPaymentResponse:
        """Проверява и прекратява активна Stripe сесия при отказ от поръчка или изтичане на резерва."""
        closure_status = "cancelled" if req.reason == "cancel" else "expired"
        try:
            self._repo.acquire_order_lock(order_id)

            if (
                self._repo.get_succeeded_attempt_by_order_for_update(order_id)
                is not None
            ):
                self._session.commit()
                raise OrderAlreadyPaid("Cannot cancel paid order")

            active_attempt = self._repo.get_active_attempt_by_order_for_update(order_id)
            if active_attempt is None:
                self._repo.record_order_closure_idempotent(
                    order_id=order_id,
                    status=closure_status,
                    reason=req.reason,
                )
                self._session.commit()
                return InternalCancelPaymentResponse(status="can_cancel", action="none")

            if req.order_expires_at is not None:
                active_attempt.order_expires_at = req.order_expires_at

            if active_attempt.status == "processing":
                self._session.commit()
                raise PaymentStateConflict(
                    current_status="processing",
                    message="Cannot cancel order while payment is processing",
                )

            session_rec = self._repo.get_stripe_session_by_attempt_id_for_update(
                active_attempt.id
            )
            stripe_session_id = (
                session_rec.stripe_session_id if session_rec is not None else None
            )

            # Изходът от загубен отговор при initiated (без stripe_session_id) не може да се докаже локално:
            # пазим резерва както при ръчна отмяна, така и при изтичане на срока.
            if active_attempt.status == "initiated" and not stripe_session_id:
                self._session.commit()
                raise PaymentStateConflict(
                    current_status="initiated",
                    message="Payment session creation outcome is unknown; reserve must be preserved.",
                )

            if not stripe_session_id:
                active_attempt.status = "expired"
                if session_rec is not None:
                    session_rec.status = "expired"
                self._repo.record_order_closure_idempotent(
                    order_id=order_id,
                    status=closure_status,
                    reason=req.reason,
                )
                self._session.commit()
                return InternalCancelPaymentResponse(
                    status="can_cancel", action="no_stripe_session"
                )

            attempt_id = active_attempt.id
            session_db_id = session_rec.id  # type: ignore[union-attr]
            active_attempt.status = "cancelling"
            session_rec.status = "cancelling"  # type: ignore[union-attr]
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        # Фаза 2: Прекратяване в Stripe извън DB заключването
        try:
            self._stripe_client.expire_checkout_session(stripe_session_id)
        except StripeNotConfigured:
            self._revert_cancelling_to_open(order_id, attempt_id, session_db_id)
            raise
        except Exception as exc:
            err_msg = str(exc).lower()
            if "already expired" in err_msg:
                pass
            elif "complete" in err_msg or "already paid" in err_msg:
                # Плащането може да е успяло в същия момент: сверяваме директно със Stripe
                self._sync_after_expire_conflict(
                    order_id, attempt_id, session_db_id, stripe_session_id
                )
            else:
                self._revert_cancelling_to_open(order_id, attempt_id, session_db_id)
                raise StripeServiceError(
                    f"Failed to expire Stripe session: {type(exc).__name__}"
                ) from exc

        # Фаза 3: Заключване и окончателно маркиране expired (освен ако междувременно webhook не е платил)
        try:
            self._repo.acquire_order_lock(order_id)
            attempt_rec = self._repo.get_attempt_by_id_for_update(attempt_id)
            session_rec = self._repo.get_stripe_session_by_id_for_update(session_db_id)

            if (attempt_rec is not None and attempt_rec.status == "succeeded") or (
                session_rec is not None and session_rec.status == "completed"
            ):
                self._session.commit()
                raise OrderAlreadyPaid(
                    "Payment completed before cancellation could finish"
                )

            if attempt_rec is not None:
                attempt_rec.status = "expired"
            if session_rec is not None:
                session_rec.status = "expired"
            self._repo.record_order_closure_idempotent(
                order_id=order_id,
                status=closure_status,
                reason=req.reason,
            )
            self._session.commit()
            return InternalCancelPaymentResponse(
                status="can_cancel",
                action="expired_in_stripe",
            )
        except Exception:
            self._session.rollback()
            raise

    def _revert_cancelling_to_open(
        self, order_id: int, attempt_id: int, session_db_id: int
    ) -> None:
        try:
            self._repo.acquire_order_lock(order_id)
            attempt_rec = self._repo.get_attempt_by_id_for_update(attempt_id)
            session_rec = self._repo.get_stripe_session_by_id_for_update(session_db_id)
            if attempt_rec is not None and attempt_rec.status == "cancelling":
                attempt_rec.status = "open"
            if session_rec is not None and session_rec.status == "cancelling":
                session_rec.status = "open"
            self._session.commit()
        except Exception:  # noqa: BLE001
            self._session.rollback()

    def _sync_after_expire_conflict(
        self,
        order_id: int,
        attempt_id: int,
        session_db_id: int,
        stripe_session_id: str,
    ) -> None:
        try:
            stripe_session = self._stripe_client.retrieve_checkout_session(
                stripe_session_id
            )
        except Exception as exc:
            self._revert_cancelling_to_open(order_id, attempt_id, session_db_id)
            raise StripeServiceError(
                f"Failed to verify Stripe session after expire conflict: {type(exc).__name__}"
            ) from exc

        payment_status = _read_session_field(stripe_session, "payment_status", None)
        stripe_state = _read_session_field(stripe_session, "status", None)

        if payment_status == "paid":
            self._repo.acquire_order_lock(order_id)
            attempt_rec = self._repo.get_attempt_by_id_for_update(attempt_id)
            session_rec = self._repo.get_stripe_session_by_id_for_update(session_db_id)
            if attempt_rec is None or session_rec is None:
                self._record_discrepancy(
                    source="cancel_conflict",
                    reason="invalid_attempt",
                    order_id=order_id,
                    payment_attempt_id=attempt_id,
                    stripe_session_id=stripe_session_id,
                )
                self._session.commit()
                raise PaymentStateConflict(
                    current_status="discrepancy",
                    message="Payment attempt not found during cancel conflict verification",
                )

            mismatch_reason = self._validate_and_record_paid_discrepancy(
                source="cancel_conflict",
                attempt=attempt_rec,
                session_rec=session_rec,
                session_obj=stripe_session,
            )
            if mismatch_reason is not None:
                if attempt_rec.status == "cancelling":
                    attempt_rec.status = "open"
                if session_rec.status == "cancelling":
                    session_rec.status = "open"
                self._session.commit()
                raise PaymentStateConflict(
                    current_status="discrepancy",
                    message=f"Stripe payment validation discrepancy ({mismatch_reason}); order remains reserved",
                )

            self._complete_verified_payment(
                attempt=attempt_rec,
                session_rec=session_rec,
                retrieved_sid=stripe_session_id,
            )
            self._session.commit()
            raise OrderAlreadyPaid("Payment completed before cancellation could finish")

        if stripe_state == "complete":
            self._repo.acquire_order_lock(order_id)
            attempt_rec = self._repo.get_attempt_by_id_for_update(attempt_id)
            session_rec = self._repo.get_stripe_session_by_id_for_update(session_db_id)
            if attempt_rec is not None:
                attempt_rec.status = "processing"
            if session_rec is not None:
                session_rec.status = "processing"
            if attempt_rec is not None:
                self._enqueue_outbox_event(
                    attempt=attempt_rec,
                    session_record=session_rec,
                    event_type="payment.processing",
                )
            self._session.commit()
            raise PaymentStateConflict(
                current_status="processing",
                message="Cannot cancel order while payment is processing",
            )

    def process_stripe_webhook(
        self, payload: bytes, sig_header: str | None
    ) -> dict[str, Any]:
        """Обработва Stripe webhook с проверка на суров подпис, livemode и запис в Outbox."""
        event_raw = self._stripe_client.construct_webhook_event(payload, sig_header)
        event: dict[str, Any] = (
            event_raw.to_dict() if hasattr(event_raw, "to_dict") else dict(event_raw)
        )
        event_id = event["id"]
        event_type = event["type"]

        if self._repo.is_stripe_event_processed(event_id):
            return {"status": "already_processed", "event_id": event_id}

        expected_livemode = settings.stripe_mode == "live"
        if event.get("livemode") != expected_livemode:
            self._record_discrepancy(
                source="webhook",
                reason="mode_mismatch",
                stripe_event_id=event_id,
                details={
                    "expected_livemode": expected_livemode,
                    "event_livemode": event.get("livemode"),
                },
            )
            return self._record_ignored_stripe_event(
                event_id=event_id,
                event_type=event_type,
                response_status="ignored",
                reason="mode_mismatch",
            )

        supported_events = {
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
            "checkout.session.expired",
        }
        if event_type not in supported_events:
            return self._record_ignored_stripe_event(
                event_id=event_id,
                event_type=event_type,
                response_status="ignored",
            )

        raw_obj = event.get("data", {}).get("object", {})
        session_data: dict[str, Any] = (
            raw_obj.to_dict() if hasattr(raw_obj, "to_dict") else dict(raw_obj)
        )
        if "livemode" in session_data and session_data["livemode"] != expected_livemode:
            self._record_discrepancy(
                source="webhook",
                reason="mode_mismatch",
                stripe_session_id=session_data.get("id"),
                stripe_event_id=event_id,
                details={
                    "expected_livemode": expected_livemode,
                    "session_livemode": session_data.get("livemode"),
                },
            )
            return self._record_ignored_stripe_event(
                event_id=event_id,
                event_type=event_type,
                response_status="ignored",
                reason="mode_mismatch",
            )

        stripe_session_id = session_data.get("id")
        metadata = session_data.get("metadata") or {}
        order_id_raw = metadata.get("order_id") or session_data.get(
            "client_reference_id"
        )
        attempt_id_raw = metadata.get("attempt_id")
        payment_attempt_id_raw = metadata.get("payment_attempt_id")

        order_id = _parse_optional_int(order_id_raw)
        session_db_id = _parse_optional_int(attempt_id_raw)
        payment_attempt_id = _parse_optional_int(payment_attempt_id_raw)

        if order_id is None and stripe_session_id:
            found = self._repo.get_stripe_session_by_stripe_id(stripe_session_id)
            if found is not None:
                order_id = found.order_id
                if session_db_id is None:
                    session_db_id = found.id
                if payment_attempt_id is None:
                    payment_attempt_id = found.payment_attempt_id

        if order_id is None or not stripe_session_id:
            return self._record_ignored_stripe_event(
                event_id=event_id,
                event_type=event_type,
                response_status="ignored",
                reason="missing_identifiers",
            )

        try:
            self._repo.acquire_order_lock(order_id)

            if self._repo.is_stripe_event_processed(event_id):
                self._session.commit()
                return {"status": "already_processed", "event_id": event_id}

            session_rec = self._resolve_session_for_webhook(
                order_id=order_id,
                session_db_id=session_db_id,
                payment_attempt_id=payment_attempt_id,
                stripe_session_id=stripe_session_id,
            )
            if session_rec is None or session_rec.order_id != order_id:
                self._record_discrepancy(
                    source="webhook",
                    reason="invalid_attempt",
                    order_id=order_id,
                    stripe_session_id=stripe_session_id,
                    stripe_event_id=event_id,
                )
                return self._record_ignored_stripe_event(
                    event_id=event_id,
                    event_type=event_type,
                    order_id=order_id,
                    response_status="mismatched",
                    reason="invalid_attempt",
                )

            attempt = self._repo.get_attempt_by_id_for_update(
                session_rec.payment_attempt_id
            )
            if attempt is None:
                self._record_discrepancy(
                    source="webhook",
                    reason="invalid_attempt",
                    order_id=order_id,
                    stripe_session_id=stripe_session_id,
                    stripe_event_id=event_id,
                )
                return self._record_ignored_stripe_event(
                    event_id=event_id,
                    event_type=event_type,
                    order_id=order_id,
                    response_status="mismatched",
                    reason="invalid_attempt",
                )

            if (
                session_rec.stripe_session_id is not None
                and session_rec.stripe_session_id != stripe_session_id
            ):
                self._record_discrepancy(
                    source="webhook",
                    reason="session_id_mismatch",
                    order_id=order_id,
                    payment_attempt_id=attempt.id,
                    stripe_session_id=stripe_session_id,
                    stripe_event_id=event_id,
                    details={
                        "expected_stripe_session_id": session_rec.stripe_session_id
                    },
                )
                return self._record_ignored_stripe_event(
                    event_id=event_id,
                    event_type=event_type,
                    order_id=order_id,
                    payment_attempt_id=attempt.id,
                    response_status="mismatched",
                    reason="session_id_mismatch",
                )

            if session_rec.mode != settings.stripe_mode:
                self._record_discrepancy(
                    source="webhook",
                    reason="attempt_mode_mismatch",
                    order_id=order_id,
                    payment_attempt_id=attempt.id,
                    stripe_session_id=stripe_session_id,
                    stripe_event_id=event_id,
                    details={
                        "expected_mode": settings.stripe_mode,
                        "attempt_mode": session_rec.mode,
                    },
                )
                return self._record_ignored_stripe_event(
                    event_id=event_id,
                    event_type=event_type,
                    order_id=order_id,
                    payment_attempt_id=attempt.id,
                    response_status="mismatched",
                    reason="attempt_mode_mismatch",
                )

            if event_type in (
                "checkout.session.completed",
                "checkout.session.async_payment_succeeded",
            ):
                if event_type == "checkout.session.completed":
                    payment_status = session_data.get("payment_status")
                    if payment_status != "paid":
                        if session_rec.stripe_session_id is None and stripe_session_id:
                            session_rec.stripe_session_id = stripe_session_id
                        if attempt.status in ("initiated", "open"):
                            attempt.status = "processing"
                            session_rec.status = "processing"
                            self._enqueue_outbox_event(
                                attempt=attempt,
                                session_record=session_rec,
                                event_type="payment.processing",
                                stripe_event_id=event_id,
                            )
                            return self._commit_processed_event(
                                event_id=event_id,
                                event_type=event_type,
                                order_id=order_id,
                                payment_attempt_id=attempt.id,
                                response_status="processing",
                            )
                        if attempt.status == "processing":
                            return self._commit_processed_event(
                                event_id=event_id,
                                event_type=event_type,
                                order_id=order_id,
                                payment_attempt_id=attempt.id,
                                response_status="processing",
                            )
                        return self._commit_processed_event(
                            event_id=event_id,
                            event_type=event_type,
                            order_id=order_id,
                            payment_attempt_id=attempt.id,
                            response_status="ignored",
                        )

                mismatch_reason = self._validate_and_record_paid_discrepancy(
                    source="webhook",
                    attempt=attempt,
                    session_rec=session_rec,
                    session_obj=session_data,
                    stripe_event_id=event_id,
                    event_livemode=event.get("livemode"),
                )
                if mismatch_reason is not None:
                    return self._record_ignored_stripe_event(
                        event_id=event_id,
                        event_type=event_type,
                        order_id=order_id,
                        payment_attempt_id=attempt.id,
                        response_status="mismatched",
                        reason=mismatch_reason,
                    )

                self._complete_verified_payment(
                    attempt=attempt,
                    session_rec=session_rec,
                    retrieved_sid=stripe_session_id,
                    stripe_event_id=event_id,
                )
                return self._commit_processed_event(
                    event_id=event_id,
                    event_type=event_type,
                    order_id=order_id,
                    payment_attempt_id=attempt.id,
                    response_status="success",
                )

            if session_rec.stripe_session_id is None and stripe_session_id:
                session_rec.stripe_session_id = stripe_session_id

            if event_type == "checkout.session.async_payment_failed":
                if attempt.status in ("initiated", "open", "processing", "cancelling"):
                    attempt.status = "failed"
                    session_rec.status = "failed"
                    self._enqueue_outbox_event(
                        attempt=attempt,
                        session_record=session_rec,
                        event_type="payment.failed",
                        stripe_event_id=event_id,
                    )
                return self._commit_processed_event(
                    event_id=event_id,
                    event_type=event_type,
                    order_id=order_id,
                    payment_attempt_id=attempt.id,
                    response_status="failed",
                )

            if event_type == "checkout.session.expired":
                if attempt.status in ("initiated", "open", "processing", "cancelling"):
                    attempt.status = "expired"
                    session_rec.status = "expired"
                    self._enqueue_outbox_event(
                        attempt=attempt,
                        session_record=session_rec,
                        event_type="payment.expired",
                        stripe_event_id=event_id,
                    )
                return self._commit_processed_event(
                    event_id=event_id,
                    event_type=event_type,
                    order_id=order_id,
                    payment_attempt_id=attempt.id,
                    response_status="expired",
                )

            return self._record_ignored_stripe_event(
                event_id=event_id,
                event_type=event_type,
                order_id=order_id,
                payment_attempt_id=attempt.id,
                response_status="ignored",
            )
        except IntegrityError:
            self._session.rollback()
            if self._repo.is_stripe_event_processed(event_id):
                return {"status": "already_processed", "event_id": event_id}
            raise
        except Exception:
            self._session.rollback()
            raise

    def _resolve_session_for_webhook(
        self,
        *,
        order_id: int,
        session_db_id: int | None,
        payment_attempt_id: int | None,
        stripe_session_id: str,
    ) -> StripeCheckoutSession | None:
        if payment_attempt_id is not None:
            return self._repo.get_stripe_session_by_attempt_id_for_update(
                payment_attempt_id
            )
        if session_db_id is not None:
            by_id = self._repo.get_stripe_session_by_id_for_update(session_db_id)
            if by_id is not None and by_id.order_id == order_id:
                return by_id
            by_stripe = self._repo.get_stripe_session_by_stripe_id_for_update(
                stripe_session_id
            )
            if by_stripe is not None and by_stripe.order_id == order_id:
                return by_stripe
            return by_id
        by_stripe = self._repo.get_stripe_session_by_stripe_id_for_update(
            stripe_session_id
        )
        if by_stripe is not None:
            return by_stripe
        return self._repo.get_latest_stripe_session_by_order_for_update(order_id)

    def _commit_processed_event(
        self,
        *,
        event_id: str,
        event_type: str,
        order_id: int,
        payment_attempt_id: int,
        response_status: str,
    ) -> dict[str, Any]:
        self._repo.record_stripe_event(
            StripeProcessedEvent(
                stripe_event_id=event_id,
                event_type=event_type,
                order_id=order_id,
                payment_attempt_id=payment_attempt_id,
            )
        )
        self._session.commit()
        return {"status": response_status, "event_id": event_id}

    def reconcile_single_attempt(
        self, attempt_id: int, current_time: datetime.datetime
    ) -> str:
        """Сверява единичен неразрешен платежен опит със Stripe API без задържане на DB lock."""
        try:
            attempt = self._repo.get_attempt_by_id_for_update(attempt_id)
            if attempt is None or attempt.status not in (
                "initiated",
                "open",
                "processing",
                "cancelling",
            ):
                self._session.commit()
                return "skipped"

            is_time_expired = (
                attempt.order_expires_at is not None
                and attempt.order_expires_at <= current_time
            )
            session_rec = self._repo.get_stripe_session_by_attempt_id_for_update(
                attempt.id
            )
            stripe_session_id = (
                session_rec.stripe_session_id if session_rec is not None else None
            )

            # При загубен отговор (initiated без stripe_session_id) изходът в Stripe е неизвестен:
            # НЕ обявяваме опитa за expired и НЕ освобождаваме резерва преди потвърждение.
            if attempt.status == "initiated" and not stripe_session_id:
                self._session.commit()
                return "deferred"

            if not stripe_session_id:
                self._session.commit()
                return "deferred"

            order_id = attempt.order_id
            session_db_id = session_rec.id  # type: ignore[union-attr]
            local_status = attempt.status
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        # Фаза 2: Четене на реалното състояние в Stripe извън DB заключване
        try:
            stripe_session = self._stripe_client.retrieve_checkout_session(
                stripe_session_id
            )
        except Exception:  # noqa: BLE001
            return "deferred"

        stripe_payment_status = _read_session_field(
            stripe_session, "payment_status", None
        )
        stripe_state = _read_session_field(stripe_session, "status", None)

        try:
            if stripe_payment_status == "paid":
                self._repo.acquire_order_lock(order_id)
                attempt_rec = self._repo.get_attempt_by_id_for_update(attempt_id)
                session_rec = self._repo.get_stripe_session_by_id_for_update(
                    session_db_id
                )
                if attempt_rec is None or session_rec is None:
                    self._record_discrepancy(
                        source="reconcile",
                        reason="invalid_attempt",
                        order_id=order_id,
                        payment_attempt_id=attempt_id,
                        stripe_session_id=stripe_session_id,
                    )
                    self._session.commit()
                    return "deferred"

                mismatch_reason = self._validate_and_record_paid_discrepancy(
                    source="reconcile",
                    attempt=attempt_rec,
                    session_rec=session_rec,
                    session_obj=stripe_session,
                )
                if mismatch_reason is not None:
                    self._session.commit()
                    return "deferred"

                self._complete_verified_payment(
                    attempt=attempt_rec,
                    session_rec=session_rec,
                    retrieved_sid=stripe_session_id,
                )
                self._session.commit()
                return "paid"

            if stripe_state == "expired":
                self._repo.acquire_order_lock(order_id)
                attempt_rec = self._repo.get_attempt_by_id_for_update(attempt_id)
                session_rec = self._repo.get_stripe_session_by_id_for_update(
                    session_db_id
                )
                if attempt_rec is not None and attempt_rec.status != "succeeded":
                    attempt_rec.status = "expired"
                    if session_rec is not None:
                        session_rec.status = "expired"
                    self._enqueue_outbox_event(
                        attempt=attempt_rec,
                        session_record=session_rec,
                        event_type="payment.expired",
                    )
                self._session.commit()
                return "expired"

            if not is_time_expired:
                if stripe_state == "complete" and stripe_payment_status != "paid":
                    self._repo.acquire_order_lock(order_id)
                    attempt_rec = self._repo.get_attempt_by_id_for_update(attempt_id)
                    session_rec = self._repo.get_stripe_session_by_id_for_update(
                        session_db_id
                    )
                    if attempt_rec is not None and attempt_rec.status not in (
                        "succeeded",
                        "processing",
                    ):
                        attempt_rec.status = "processing"
                        if session_rec is not None:
                            session_rec.status = "processing"
                        self._enqueue_outbox_event(
                            attempt=attempt_rec,
                            session_record=session_rec,
                            event_type="payment.processing",
                        )
                    self._session.commit()
                return "deferred"

            if local_status == "processing" or (
                stripe_state == "complete" and stripe_payment_status != "paid"
            ):
                return "deferred"

            if stripe_state == "open":
                try:
                    self._stripe_client.expire_checkout_session(stripe_session_id)
                except Exception:  # noqa: BLE001
                    return "deferred"

            self._repo.acquire_order_lock(order_id)
            attempt_rec = self._repo.get_attempt_by_id_for_update(attempt_id)
            session_rec = self._repo.get_stripe_session_by_id_for_update(session_db_id)
            if attempt_rec is not None and attempt_rec.status != "succeeded":
                attempt_rec.status = "expired"
                if session_rec is not None:
                    session_rec.status = "expired"
                self._enqueue_outbox_event(
                    attempt=attempt_rec,
                    session_record=session_rec,
                    event_type="payment.expired",
                )
                self._session.commit()
                return "expired"

            self._session.commit()
            return "deferred"
        except Exception:
            self._session.rollback()
            raise

    def reconcile_unresolved_payments(
        self, now: datetime.datetime | None = None
    ) -> dict[str, int]:
        """Сверява всички незавършени платежни опити в Payments."""
        current_time = now or datetime.datetime.now(datetime.UTC)
        attempt_ids = self._repo.list_unresolved_attempt_ids()
        stats = {
            "checked": len(attempt_ids),
            "expired": 0,
            "paid": 0,
            "deferred": 0,
            "skipped": 0,
        }
        for attempt_id in attempt_ids:
            try:
                res = self.reconcile_single_attempt(attempt_id, current_time)
                if res in stats:
                    stats[res] += 1
            except Exception:  # noqa: BLE001
                stats["deferred"] += 1
        return stats


def _read_session_field(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        as_dict = obj.to_dict()
        if isinstance(as_dict, dict):
            return as_dict.get(key, default)
    return getattr(obj, key, default)


def _parse_optional_int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
