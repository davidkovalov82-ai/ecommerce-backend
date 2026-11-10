import datetime
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.orders.models import (
    OrderInboxEvent,
    OrderPaymentDiscrepancy,
    PaymentAttempt,
)
from tests.payments.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
)


def test_payment_attempts_database_constraints(db_session: Session) -> None:
    """Критерий: UNIQUE(order_id, idempotency_key), частичен индекс и CHECK ограничения."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product)

    # 1. Валиден опит за плащане
    attempt1 = PaymentAttempt(
        order_id=order.id,
        payment_method="test_declined",
        status="declined",
        amount_minor=1000,
        currency="EUR",
        idempotency_key="key-db-1",
    )
    db_session.add(attempt1)
    db_session.commit()

    # 2. UNIQUE(order_id, idempotency_key) дубликат
    duplicate_key_attempt = PaymentAttempt(
        order_id=order.id,
        payment_method="test_success",
        status="succeeded",
        amount_minor=1000,
        currency="EUR",
        idempotency_key="key-db-1",
    )
    db_session.add(duplicate_key_attempt)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 3. Един успешен опит
    success_attempt = PaymentAttempt(
        order_id=order.id,
        payment_method="test_success",
        status="succeeded",
        amount_minor=1000,
        currency="EUR",
        idempotency_key="key-db-2",
    )
    db_session.add(success_attempt)
    db_session.commit()

    # 4. Частичен уникален индекс: втори успешен опит за същата поръчка се отхвърля
    second_success_attempt = PaymentAttempt(
        order_id=order.id,
        payment_method="test_success",
        status="succeeded",
        amount_minor=1000,
        currency="EUR",
        idempotency_key="key-db-3",
    )
    db_session.add(second_success_attempt)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 5. CHECK status IN ('succeeded', 'declined')
    invalid_status_attempt = PaymentAttempt(
        order_id=order.id,
        payment_method="test_success",
        status="pending",
        amount_minor=1000,
        currency="EUR",
        idempotency_key="key-db-4",
    )
    db_session.add(invalid_status_attempt)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 6. CHECK currency = 'EUR'
    invalid_currency_attempt = PaymentAttempt(
        order_id=order.id,
        payment_method="test_success",
        status="succeeded",
        amount_minor=1000,
        currency="USD",
        idempotency_key="key-db-5",
    )
    db_session.add(invalid_currency_attempt)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_inbox_and_discrepancy_database_constraints(db_session: Session) -> None:
    """Проверява уникалността на order_inbox_events и ограниченията на order_payment_discrepancies."""
    user, _ = _create_user_and_token(db_session, email="inbox_db@example.com")
    product = _create_product(db_session, title="Inbox Item")
    order = _create_order(db_session, user.id, product, status="cancelled")

    ev_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.UTC)

    inbox1 = OrderInboxEvent(
        event_id=ev_id,
        event_type="payment.succeeded",
        event_version=1,
        payment_attempt_id=10,
        order_id=order.id,
        outcome="discrepancy",
        payload={"order_id": order.id},
        occurred_at=now,
    )
    db_session.add(inbox1)
    db_session.commit()
    db_session.expunge_all()

    inbox_dup = OrderInboxEvent(
        event_id=ev_id,
        event_type="payment.succeeded",
        event_version=1,
        payment_attempt_id=10,
        order_id=order.id,
        outcome="discrepancy",
        payload={"order_id": order.id},
        occurred_at=now,
    )
    db_session.add(inbox_dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    d1 = OrderPaymentDiscrepancy(
        order_id=order.id,
        payment_attempt_id=10,
        event_id=ev_id,
        reason="payment_on_cancelled_order",
        amount_minor=2500,
        currency="EUR",
        status="open",
        details={"source": "test"},
    )
    db_session.add(d1)
    db_session.commit()

    d_bad_status = OrderPaymentDiscrepancy(
        order_id=order.id,
        payment_attempt_id=11,
        event_id=str(uuid.uuid4()),
        reason="payment_on_cancelled_order",
        amount_minor=2500,
        currency="EUR",
        status="invalid_status",
        details={},
    )
    db_session.add(d_bad_status)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
