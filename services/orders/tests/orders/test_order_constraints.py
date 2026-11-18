import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.orders.models import Order, OrderItem
from tests.orders.helpers import _create_product, _create_user_and_token


def test_orders_database_constraints(db_session: Session) -> None:
    """Критерий: UNIQUE, CHECK и FOREIGN KEY ограниченията в orders и order_items работят на ниво PostgreSQL."""
    user, _ = _create_user_and_token(db_session)
    _create_product(db_session)

    # 1. Валидна поръчка
    valid_order = Order(
        user_id=user.id,
        status="pending_payment",
        currency="EUR",
        total_minor=1000,
        idempotency_key="key-1",
    )
    db_session.add(valid_order)
    db_session.commit()

    # 2. UNIQUE(user_id, idempotency_key) дубликат
    duplicate_key_order = Order(
        user_id=user.id,
        status="pending_payment",
        currency="EUR",
        total_minor=2000,
        idempotency_key="key-1",
    )
    db_session.add(duplicate_key_order)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 3. CHECK status = 'pending_payment'
    invalid_status_order = Order(
        user_id=user.id,
        status="shipped",
        currency="EUR",
        total_minor=1000,
        idempotency_key="key-2",
    )
    db_session.add(invalid_status_order)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 4. CHECK currency = 'EUR'
    invalid_currency_order = Order(
        user_id=user.id,
        status="pending_payment",
        currency="USD",
        total_minor=1000,
        idempotency_key="key-3",
    )
    db_session.add(invalid_currency_order)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 5. В Orders DB няма FK към users: поръчката се пази по логически user_id
    foreign_user_id = 999999
    order_foreign_user = Order(
        user_id=foreign_user_id,
        status="pending_payment",
        currency="EUR",
        total_minor=1000,
        idempotency_key="key-foreign-user",
    )
    db_session.add(order_foreign_user)
    db_session.commit()
    persisted_order = db_session.get(Order, order_foreign_user.id)
    assert persisted_order is not None
    assert persisted_order.user_id == foreign_user_id

    # 6. В Orders DB няма FK към products: поръчката пази снимка и логическо product_id
    order_item = OrderItem(
        order_id=valid_order.id,
        product_id=888888,
        product_title="Non-existent product",
        quantity=1,
        unit_price_minor=1000,
        subtotal_minor=1000,
    )
    db_session.add(order_item)
    db_session.commit()
    persisted_item = (
        db_session.query(OrderItem).filter(OrderItem.order_id == valid_order.id).first()
    )
    assert persisted_item is not None
    assert persisted_item.product_title == "Non-existent product"
