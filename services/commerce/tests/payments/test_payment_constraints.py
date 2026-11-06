import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.orders.models import PaymentAttempt
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
